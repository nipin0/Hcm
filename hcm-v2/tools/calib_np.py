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
