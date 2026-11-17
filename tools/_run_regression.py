"""统一回归测试：跑 tools/ 下所有回归脚本，汇总结果"""
import glob
import io
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 项目根
os.chdir(ROOT)
PY = os.path.join(ROOT, "_pyenv", "venv", "Scripts", "python.exe")

SCRIPTS = sorted(
    glob.glob(os.path.join("tools", "_test_*.py"))
    + glob.glob(os.path.join("tools", "_test_release*.py"))
    + glob.glob(os.path.join("scripts", "verify_v3010_regression.py"))
    + glob.glob(os.path.join("tools", "verify_v3010_regression.py"))
    + glob.glob("verify_v3010_regression.py")
)
# 去重
seen = set()
uniq = []
for s in SCRIPTS:
    if s not in seen:
        seen.add(s)
        uniq.append(s)

out = io.open("_regression_out.txt", "w", encoding="utf-8")
results = []

env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"

for s in uniq:
    name = os.path.basename(s)
    t0 = time.time()
    try:
        p = subprocess.run(
            [PY, s], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=300, env=env, cwd=ROOT,
        )
        code = p.returncode
        outp = (p.stdout or "")[-3000:]
        errp = (p.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        code, outp, errp = -1, "", "TIMEOUT(300s)"
    except Exception as e:
        code, outp, errp = -1, "", str(e)
    dt = time.time() - t0
    results.append((name, code, dt, outp, errp))
    out.write(f"\n{'='*70}\n### {name}  exit={code}  {dt:.1f}s\n{'='*70}\n")
    out.write(outp + "\n")
    if errp.strip():
        out.write("--- STDERR ---\n" + errp + "\n")

print(f"{'脚本':<42}{'退出码':>8}{'耗时':>8}")
print("-" * 60)
npass = nfail = 0
for name, code, dt, _, _ in results:
    tag = "PASS" if code == 0 else f"FAIL({code})"
    if code == 0:
        npass += 1
    else:
        nfail += 1
    print(f"{name:<42}{tag:>8}{dt:>7.1f}s")
print("-" * 60)
print(f"合计: {npass} 通过 / {nfail} 失败 / 共 {len(results)}")
out.close()
