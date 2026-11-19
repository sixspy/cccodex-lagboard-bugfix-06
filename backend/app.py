
import hashlib, json, secrets, sqlite3, threading, time
from pathlib import Path
from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

# 冻写切断（freeze-write cut）：冻结徽标仍收报文时必须切断入库。
# 口径 = 甲路径（整笔锁行拒插 + 审计 + 正式零增），不得与乙路径草稿混用。
# 拒写时冻写锁的驻留秒数：默认 0（真实并发才记争用），失败测调大以稳定复现双席争用。
FREEZE_LOCK_HOLD_SECONDS = 0.0
FREEZE_LOCK_GRACE_SECONDS = 5.0
_FREEZE_LOCKS: dict[str, threading.Lock] = {}
_FREEZE_LOCKS_GUARD = threading.Lock()

# 冻写切断卡封档：冻结当天不许改口切断专册，改口即判偏离并留「冻写偏了」。
CARD_DOC = ROOT / "docs" / "冻写切断卡.md"
CARD_SEAL = ROOT / "docs" / "freeze-cut-card.封档.json"

def freeze_lock_for(source: str) -> threading.Lock:
    """同一冻结源一把冻写锁；调度席与采集席抢入时后交拿不到锁。"""
    with _FREEZE_LOCKS_GUARD:
        return _FREEZE_LOCKS.setdefault(source, threading.Lock())

def card_deviation() -> str | None:
    """冻写切断卡封档校验；封档失配或口径混用时返回偏离原因。"""
    try:
        sealed = json.loads(CARD_SEAL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "冻写切断卡封档缺失或不可读"
    if sealed.get("status") != "钉死":
        return "切断卡未钉死"
    if sealed.get("paths") != ["甲"]:
        return "甲乙口径混用"
    digest = hashlib.sha256(CARD_DOC.read_bytes()).hexdigest()
    if digest != sealed.get("sha256"):
        return "切断专册被改口"
    return None

def connect():
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn

def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()

def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))

class LoginIn(BaseModel):
    username: str
    password: str

def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u

from datetime import datetime
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

PRODUCT = "源到达SLA"
SKELETON = "多源到达时钟对照 SLA 分级，超限则冻结或降级输出，补齐后恢复并对账"

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

def init_db():
    with connect() as conn:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS sources(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT UNIQUE NOT NULL,
          sla_minutes INTEGER NOT NULL,
          output_mode TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS arrivals(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          source_clock TEXT NOT NULL,
          ingest_clock TEXT NOT NULL,
          lag_minutes INTEGER NOT NULL,
          level TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS catchups(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          status TEXT NOT NULL,
          restored_mode TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS freeze_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL,
          action TEXT NOT NULL,
          mode TEXT NOT NULL,
          seat TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        """)
        if conn.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"] == 0:
            conn.executemany("INSERT INTO sources(name,sla_minutes,output_mode) VALUES (?,?,?)", [
                ("订单源", 15, "可写"), ("支付源", 20, "降级只读"), ("物流源", 30, "冻结"),
            ])
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("支付源","2026-11-16T08:00:00","2026-11-16T08:35:00",35,"告警"))
            conn.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
                         ("物流源","2026-11-16T07:00:00","2026-11-16T09:10:00",130,"违约"))

def level_of(lag, sla):
    if lag <= sla: return "正常"
    if lag <= sla * 2: return "告警"
    return "违约"

def mode_of(level):
    return {"正常":"可写", "告警":"降级只读", "违约":"冻结"}[level]

@app.on_event("startup")
def _s(): init_db()

@app.get("/api/health")
def health():
    with connect() as c:
        n = c.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "sources": n, "db": str(DB_PATH)}

@app.post("/api/login")
def login(body: LoginIn):
    with connect() as c:
        row = c.execute("SELECT username,role FROM users WHERE username=? AND password_sha=?", (body.username, sha(body.password))).fetchone()
    if not row: raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16); SESSIONS[tok]=row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}

@app.get("/api/sources")
def sources(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM sources ORDER BY id").fetchall()]}

class ArrivalIn(BaseModel):
    source: str
    source_clock: str
    ingest_clock: str

def _cut_response(code: str, action: str, source: str, detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=423,
        content={
            "ok": False,
            "code": code,
            "inserted": False,
            "audit_action": action,
            "source": source,
            "output_mode": "冻结",
            "detail": detail,
        },
    )

def _audit(conn, source: str, action: str, seat: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO freeze_audit(source,action,mode,seat,detail,created_at) VALUES (?,?,?,?,?,?)",
        (source, action, "冻结", seat, detail, datetime.now().isoformat(timespec="seconds")),
    )

def _refuse_frozen(conn, src, seat: str, detail: str, action: str = "拒写") -> JSONResponse:
    """甲路径：整笔锁行拒插，审计留痕，正式到达零增。"""
    _audit(conn, src["name"], action, seat, detail)
    conn.commit()
    code = "FREEZE_CONTENTION" if action == "冻写争用" else "FREEZE_REFUSED"
    return _cut_response(code, action, src["name"], detail)

def _ingest(c, body: ArrivalIn, src) -> JSONResponse:
    """在已持有冻写锁的连接上完成一次到达入库。"""
    def parse(t):
        return datetime.fromisoformat(t)
    lag = int((parse(body.ingest_clock) - parse(body.source_clock)).total_seconds() // 60)
    if lag < 0: lag = 0
    lv = level_of(lag, src["sla_minutes"])
    mode = mode_of(lv)
    c.execute("INSERT INTO arrivals(source,source_clock,ingest_clock,lag_minutes,level) VALUES (?,?,?,?,?)",
              (body.source, body.source_clock, body.ingest_clock, lag, lv))
    c.execute("UPDATE sources SET output_mode=? WHERE name=?", (mode, body.source))
    c.commit()
    return JSONResponse(status_code=200, content={
        "ok": True, "inserted": True, "lag_minutes": lag, "level": lv,
        "output_mode": mode, "source": body.source,
    })

@app.post("/api/arrivals")
def add_arrival(body: ArrivalIn, authorization: str | None = Header(default=None)):
    seat = require_user(authorization)
    deviation = card_deviation()
    if deviation:
        with connect() as c:
            _audit(c, body.source, "冻写偏了", seat, deviation)
            c.commit()
        return _cut_response("FREEZE_CARD_DEVIATION", "冻写偏了", body.source, deviation)
    with connect() as c:
        src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
    if not src: raise HTTPException(404, "未知源")

    guard = freeze_lock_for(body.source)
    if src["output_mode"] == "冻结":
        # 冻结徽标仍收报文：先占冻写锁；抢不到锁的后来者只记冻写争用。
        if not guard.acquire(blocking=False):
            with connect() as c:
                now_src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
            if now_src and now_src["output_mode"] == "冻结":
                with connect() as c:
                    return _refuse_frozen(c, now_src, seat, "双席抢入冻结源，后交不落库", "冻写争用")
            if not guard.acquire(timeout=FREEZE_LOCK_GRACE_SECONDS):
                with connect() as c:
                    now_src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
                    if now_src and now_src["output_mode"] == "冻结":
                        return _refuse_frozen(c, now_src, seat, "冻写锁超时未释放，后交不落库", "冻写争用")
                guard.acquire()
    else:
        guard.acquire()

    try:
        with connect() as c:
            c.execute("BEGIN IMMEDIATE")
            src = c.execute("SELECT * FROM sources WHERE name=?", (body.source,)).fetchone()
            if not src:
                c.rollback()
                raise HTTPException(404, "未知源")
            if src["output_mode"] == "冻结":
                resp = _refuse_frozen(c, src, seat, "冻结徽标仍收报文，整笔锁行拒插")
                time.sleep(FREEZE_LOCK_HOLD_SECONDS)
                return resp
            return _ingest(c, body, src)
    finally:
        guard.release()

@app.get("/api/arrivals")
def arrivals(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM arrivals ORDER BY id DESC").fetchall()]}

@app.get("/api/freeze-audit")
def freeze_audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM freeze_audit ORDER BY id").fetchall()]}

@app.get("/api/tasks")
def tasks(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        rows = c.execute("SELECT * FROM sources ORDER BY id").fetchall()
        return {"items":[{
            "source": r["name"],
            "output_mode": r["output_mode"],
            "freeze_cut": r["output_mode"] == "冻结",
            "ingest_allowed": r["output_mode"] != "冻结",
        } for r in rows]}

@app.get("/api/watermark")
def watermark(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        def n(table, where="", args=()):
            return c.execute(f"SELECT COUNT(*) c FROM {table} {where}", args).fetchone()["c"]
        return {
            "arrivals": n("arrivals"),
            "sources": n("sources"),
            "catchups": n("catchups"),
            "freeze_refused": n("freeze_audit", "WHERE action='拒写'"),
            "freeze_contention": n("freeze_audit", "WHERE action='冻写争用'"),
        }

@app.post("/api/catchup/{source}")
def catchup(source: str, authorization: str | None = Header(default=None)):
    require_user(authorization)
    now = datetime.now().isoformat(timespec="seconds")
    guard = freeze_lock_for(source)
    with guard:
        with connect() as c:
            c.execute("BEGIN IMMEDIATE")
            src = c.execute("SELECT * FROM sources WHERE name=?", (source,)).fetchone()
            if not src:
                c.rollback()
                raise HTTPException(404, "未知源")
            c.execute("UPDATE sources SET output_mode='可写' WHERE name=?", (source,))
            c.execute("INSERT INTO catchups(source,status,restored_mode,note,created_at) VALUES (?,?,?,?,?)",
                      (source, "成功", "可写", "补齐后恢复可写", now))
            c.commit()
    return {"ok": True, "output_mode": "可写"}

@app.get("/api/catchups")
def catchups(authorization: str | None = Header(default=None)):
    require_user(authorization)
    with connect() as c:
        return {"items":[dict(r) for r in c.execute("SELECT * FROM catchups ORDER BY id DESC").fetchall()]}

@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
