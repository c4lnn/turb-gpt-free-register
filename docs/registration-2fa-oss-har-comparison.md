# OSS 与 HAR 对照：统一入口

OSS、本地仓库与两份 HAR 的逐端点对照统一维护于[当前注册实现与两份独立 HAR 对照](registration-2fa-har-comparison.md)。该文按 HAR A / HAR B 分开列出每个请求的顺序、headers、body、响应要点、本地实现、OSS 实现及差异；本页不再保留重复矩阵，避免两份证据表失去同步。

OSS 源码自身的注册、callback/session 和 2FA 分支说明见[注册与 2FA OSS 源码对照](registration-2fa-oss-comparison.md)。HAR 证据边界、敏感数据处理和源码快照说明以统一逐端点对照为准。
