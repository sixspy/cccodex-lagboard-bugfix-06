"""为冻写切断卡封档：写入 docs/freeze-cut-card.封档.json。

冻结当天（2026-11-19）钉死后执行一次；此后改 docs/冻写切断卡.md 正文
即令封档哈希失配，入库路径转红并留「冻写偏了」。

跑法（仓库根目录）：
    python scripts/seal_freeze_card.py [--force]
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CARD_DOC = ROOT / "docs" / "冻写切断卡.md"
CARD_SEAL = ROOT / "docs" / "freeze-cut-card.封档.json"
PINNED_ON = "2026-11-19"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="封档已存在时仍重封")
    args = parser.parse_args()

    if CARD_SEAL.exists() and not args.force:
        print(f"已封档：{CARD_SEAL}（冻结当天不许改口；确需重封用 --force）")
        return 1
    if datetime.now().date().isoformat() != PINNED_ON and not args.force:
        print(f"仅允许钉死当天（{PINNED_ON}）封档；其它日期须 --force 并说明改口原因")
        return 1

    seal = {
        "card": CARD_DOC.name,
        "code": "FWC-06-01",
        "status": "钉死",
        "paths": ["甲"],
        "pinned_on": PINNED_ON,
        "sealed_at": datetime.now().isoformat(timespec="seconds"),
        "sha256": hashlib.sha256(CARD_DOC.read_bytes()).hexdigest(),
    }
    CARD_SEAL.write_text(json.dumps(seal, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"封档完成：{CARD_SEAL}")
    print(seal["sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
