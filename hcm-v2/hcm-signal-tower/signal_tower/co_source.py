"""【已下线·占位】原「共源信号增强引擎」(Co-Source Signal Enhancer)。

【2026-08-28 双源信号模式清除】原 CoSourceEngine（v1 apply / v2 apply_v2 收敛决策）
整体下线，系统只保留 HEXP（乘幂）引擎。

【2026-09-11 反冗余】其最后遗留的共享常量 ``_CALIB_KEYS``（Regime 五态 → co.calib.*
配置键映射）随「co.calib.* 写回链」一并删除——该链路 write-only、全仓无任何消费端。
本文件现已无任何导出符号。

保留本空文件的原因（架构约束，非历史遗留）：docker-compose.yml 将本文件 bind mount
进容器；若删除文件，Docker 会因挂载源不存在而 **OCI runtime create failed** 无法启动
（2026-08-28 实测）。故保留文件名作占位。
"""
