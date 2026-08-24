# -*- coding: utf-8 -*-
"""
2FA（TOTP）配置

是否在注册成功后自动设置 2FA：
    True:  注册完成 → 拉新 OTP 邮件 → enroll TOTP → activate → 把 secret 写入 DB。
           protocol 使用 BrowserSession；Roxy 使用当前 Selenium/Roxy Profile 的页面上下文，
           两者不共享 Cookie、设备 ID 或请求会话。
    False: 跳过整个 2FA 流程，只保存 邮箱 + accessToken

关掉 2FA 不会影响账号可用性，仅意味着账号没有动态口令保护，且少收一封 OTP 邮件。
Roxy 2FA 失败时账号仍会保存，并在账号 extra.twofa 中记录脱敏状态和错误摘要，不会伪造 TOTP Secret。
"""
from config.env_loader import apply_env_overrides

ENABLE_2FA = False

# ---- .env overrides for WebUI editable fields ----
apply_env_overrides(globals(), {'ENABLE_2FA': 'bool'})
