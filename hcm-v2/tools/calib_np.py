"""纯 numpy 单调概率校准器（等价于 sklearn IsotonicRegression(out_of_bounds='clip')）。

【2026-08-28 校准器修复】原 calib_v53.pkl 为空壳(None)，calib_final.pkl 为 sklearn
IsotonicRegression 但生产环境缺 sklearn 无法加载 → AI 分长期未校准(raw prob)。
本模块用 PAVA 拟合单调映射，pickle 序列化后**仅依赖 numpy**即可在生产反序列化，
彻底绕开 sklearn 缺失死结。与 quality_scorer.score_one 的 `iso.predict([p_raw])[0]`
接口完全兼容。无 y_thresholds_ 属性 → _calib_is_degenerate 返回 False（不混合）。

独立成模块的目的：fit 脚本与生产 quality_scorer 共用同一类定义，pickle 序列化的
限定名一致(calib_np.NumpyCalibrator)，避免 __main__ 命名空间不匹配导致反序列化失败。
"""
import numpy as np


class NumpyCalibrator:
    def __init__(self, x_fit, y_fit, y_min=0.05, y_max=0.95):
        self.x_fit = np.asarray(x_fit, float)
        self.y_fit = np.asarray(y_fit, float)
        self.y_min = y_min
        self.y_max = y_max

    def predict(self, X):
        X = np.asarray(X, float).ravel()
        idx = np.searchsorted(self.x_fit, X, side="right") - 1
        idx = np.clip(idx, 0, len(self.x_fit) - 1)
        return np.clip(self.y_fit[idx], self.y_min, self.y_max)

    def level_count(self) -> int:
        """映射的输出档位数（阶梯数）—— 退化判据（见 reviewer._cal_levels）。"""
        return int(len(np.unique(np.round(self.y_fit, 4))))


class PlattCalibrator:
    """参数化（sigmoid）概率校准器 —— 纯 numpy，生产容器可加载。

    【2026-09-11 B 方案】NumpyCalibrator(isotonic) 是**阶梯函数**，其分辨率受校准
    样本量硬约束：实测校准集 292 样本时仅产出 5~9 档，撞上生产门槛「档位 ≥ 8」。
    Platt scaling 只有 2 个参数 (a, b)，小样本下更稳，且输出**连续**、无档位限制。

    形式：p_cal = sigmoid(a · logit(p_raw) + b)
      · logit(p) = ln(p / (1−p))，p 先截断到 [eps, 1−eps] 防 log(0)；
      · a > 0 保持单调；a ≈ 0 ⇒ 输出近乎常数 ⇒ 视为**退化**（见 level_count）。
    """

    def __init__(self, a: float, b: float, eps: float = 1e-6):
        self.a = float(a)
        self.b = float(b)
        self.eps = float(eps)

    def predict(self, X):
        X = np.asarray(X, float).ravel()
        p = np.clip(X, self.eps, 1.0 - self.eps)
        z = np.log(p / (1.0 - p))
        t = np.clip(self.a * z + self.b, -30.0, 30.0)   # 防 exp 溢出
        return 1.0 / (1.0 + np.exp(-t))

    def level_count(self, n: int = 100) -> int:
        """与 isotonic 语义对齐的「分辨率」判据：在 [0,1] 网格上的输出档位数。

        阶梯函数用阶梯数；连续函数用同一网格上的量化分辨率 —— 两者都表达
        「校准映射是否退化（近乎常数）」。a≈0 时输出恒定 → 返回 1（判退化）。
        """
        grid = np.linspace(0.0, 1.0, n)
        return int(len(np.unique(np.round(self.predict(grid), 4))))
