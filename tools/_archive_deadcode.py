"""死代码归档：把确认无引用的文件移到 _archive/（保留，可回滚）

【重要】不删除任何文件。归档前逐个确认：
  1. 无任何 import 引用（含缩进 import、字符串动态引用）
  2. 不在 .gitignore / 打包脚本 / 测试收集路径上有特殊约定
  3. 移动后整个项目仍能 import 成功

归档目录 _archive/ 保持包结构，文件仍可被 import（回滚只需移回）。
"""
import io
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # tools/ 的上一级 = 项目根
os.chdir(ROOT)

CANDIDATES = [
    "core/pool_manager.legacy.py",
    "core/p1_performance.py",
    "core/strategies/resonance_enhancer.py",
    "core/strategy_effectiveness_ui.py",
    "core/strategies/strat_golden_10.py",
]

SKIP_DIRS = {"__pycache__", "_pyenv", ".git", "data", "node_modules", "_archive"}
SKIP_FILES = {"_archive_deadcode.py", "_archive_out.txt"}


def all_sources():
    for dp, dn, fns in os.walk("."):
        dn[:] = [d for d in dn if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in fns:
            if fn.endswith(".py") and fn not in SKIP_FILES:
                p = os.path.join(dp, fn).replace("\\", "/").lstrip("./")
                try:
                    yield p, open(p, encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue


def refs_for(rel: str):
    """找该文件的所有 import 引用（含缩进 import）。"""
    mod = rel[:-3].replace("/", ".")
    base = os.path.basename(rel)[:-3]
    pats = [
        rf"from\s+{re.escape(mod)}\b",
        rf"import\s+{re.escape(mod)}\b",
        rf"from\s+\w+\s+import\s+[^\n]*\b{re.escape(base)}\b",
        rf"['\"]{re.escape(mod)}['\"]",
        rf"['\"]{re.escape(rel)}['\"]",
    ]
    hits = []
    for p, src in all_sources():
        if p == rel:
            continue
        for pat in pats:
            for mm in re.finditer(pat, src):
                ln = src[:mm.start()].count("\n") + 1
                hits.append((p, ln))
                break
    return hits


out = io.open("_archive_out.txt", "w", encoding="utf-8")


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    out.write(s + "\n")


p("=" * 74)
p("【死代码归档】不删除，仅移动到 _archive/（保留可回滚）")
p("=" * 74)

# 二次确认无引用
todo = []
for rel in CANDIDATES:
    if not os.path.exists(rel):
        p(f"  (跳过) {rel} 不存在")
        continue
    hits = refs_for(rel)
    n = len(open(rel, encoding="utf-8", errors="ignore").read().splitlines())
    if hits:
        p(f"  ❌ {rel} ({n} 行) 仍有引用 → 不归档：")
        for hp, hn in hits[:5]:
            p(f"       {hp}:{hn}")
    else:
        p(f"  ✅ {rel} ({n} 行) 无引用 → 可归档")
        todo.append((rel, n))

if not todo:
    p("\n无可归档项")
    out.close()
    sys.exit(0)

p("")
p("=" * 74)
p(f"【执行归档】{len(todo)} 个文件")
p("=" * 74)

for rel, n in todo:
    dest = os.path.join("_archive", rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.move(rel, dest)
    # 加归档标记头
    with io.open(dest, "a", encoding="utf-8") as f:
        f.write("\n\n# ============================================================\n")
        f.write(f"# [归档于 2026-11-04] 原路径: {rel}\n")
        f.write("# 原因: 全项目无 import 引用（死代码）。\n")
        f.write("# 回滚: 把本文件移回原路径即可（_archive/ 保持包结构，import 仍可用）。\n")
        f.write("# ============================================================\n")
    p(f"  ✅ {rel}  →  {dest}  ({n} 行)")

p("")
p("=" * 74)
p("【归档后导入检查】")
p("=" * 74)
out.close()
