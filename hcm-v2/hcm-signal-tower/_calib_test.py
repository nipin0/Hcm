"""临时单测：验证 local_calibrator 纯函数逻辑（不连 DB）。"""
import sys, os
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE); sys.path.insert(0, ROOT)

from local_calibrator import _win_rate_to_calib, _regime_key

ok = True
def chk(name, got, exp):
    global ok
    status = "PASS" if abs(got - exp) < 1e-9 else "FAIL"
    if status == "FAIL": ok = False
    print(f"[{status}] {name}: got={got} exp={exp}")

def chk_eq(name, got, exp):
    global ok
    status = "PASS" if got == exp else "FAIL"
    if status == "FAIL": ok = False
    print(f"[{status}] {name}: got={got!r} exp={exp!r}")

chk("win0.7,n100", _win_rate_to_calib(0.7, 100), 1.2)
chk("win0.3,n100", _win_rate_to_calib(0.3, 100), 0.8)
chk("win0.5,n100", _win_rate_to_calib(0.5, 100), 1.0)
chk("win0.9,clamp", _win_rate_to_calib(0.9, 100), 1.4)
chk("win0.1,clamp", _win_rate_to_calib(0.1, 100), 0.6)
chk("win0.7,lowN", _win_rate_to_calib(0.7, 5), 1.0)   # 样本不足→1.0

chk_eq("key TREND", _regime_key("TREND"), "co.calib.trend")
chk_eq("key PRE_TREND", _regime_key("PRE_TREND"), "co.calib.pre_trend")
chk_eq("key TREND_FADE", _regime_key("trend_fade"), "co.calib.trend_fade")
chk_eq("key RANGE", _regime_key("Range"), "co.calib.range")
chk_eq("key NEUTRAL", _regime_key("NEUTRAL"), "co.calib.neutral")
chk_eq("key garbage", _regime_key("xxx"), None)

print("ALL_PASS" if ok else "SOME_FAIL")
