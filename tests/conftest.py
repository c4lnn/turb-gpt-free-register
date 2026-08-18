import os


# 测试默认不触碰项目根目录的真实 runtime.db；SQLite 专项测试显式覆盖该变量。
os.environ["RUNTIME_STORAGE_BACKEND"] = "json"
