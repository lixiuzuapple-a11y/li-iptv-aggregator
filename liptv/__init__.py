"""Li IPTV Aggregator — V1 最小工程骨架。

模块划分：
    db         SQLite 连接与 schema 初始化
    config     TOML 配置加载
    m3u        最小 M3U parser / generator
    repo       8 张业务表的数据访问
    select     线路评分与选择
    cli        命令行入口
"""

__version__ = "0.1.0"
