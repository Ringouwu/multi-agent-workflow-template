# 嵌入式项目示例：ESP32-C3 自定义固件

这是**按项目定制 SOUL.md 的示范**——展示如何把泛用角色模板改成具体领域的角色。

## 改了什么

| 角色 | 泛用版 | 嵌入式示例版 |
|------|--------|-------------|
| architect | 系统架构师 | 嵌入式系统架构师（选芯片/模块/库，考虑功耗/内存/稳定性） |
| builder | 实现工程师 | 嵌入式固件工程师（内存安全、非阻塞、重试降级、硬件说明） |
| auditor | 代码审计专家 | 嵌入式代码审计专家（找 bug/安全漏洞/可靠性问题，P0-P3） |

## 定制方法

1. 复制 `profiles/templates/<role>_SOUL.md` 到 `~/.hermes/profiles/<role>/SOUL.md`
2. 修改角色描述段落（第一段 + 职责列表），加入领域特定要求
3. **保留所有流程规则**（调研先行、审计闭环、完成标准、防误判）——这些是通用的，别删

## 嵌入式领域常见的定制点

- **architect**：加"硬件项目要特别考虑功耗/内存/稳定性/异常恢复"；调研重点搜硬件已知缺陷
- **builder**：加内存安全、非阻塞、重试降级、密钥不硬编码
- **auditor**：加可靠性检查（内存泄漏/死循环/边界情况）

## 参考：实际项目踩过的坑（已验证）

> 这些是**具体项目经验**，供参考——每个新项目都要由 architect 通过"调研先行"重新验证，不要盲信。

- ESP32-C3 SuperMini 的 WiFi 射频缺陷：高功率（19.5dBm）信号失真导致握手失败（status=6），需 `setTxPower(WIFI_POWER_8_5dBm)`；AP 模式不能先 `WIFI_OFF`；`softAPConfig` 必须在 `setTxPower` 之前
- 擦除 Flash 会破坏射频校准数据：`softAP()` 返回 SUCCESS 但 beacon 发不出去，强制 setTxPower 修复
- WPA1 老加密不兼容：扫描能看到但连不上（status=6），选 WPA2/混合的 2.4G
- Arduino IDE 需要 `.ino` 主文件（PlatformIO 用 main.cpp）；USB CDC On Boot 必须 Enabled 才有串口日志
- OLED 烧屏：像素偏移 + 低对比度是社区验证组合
