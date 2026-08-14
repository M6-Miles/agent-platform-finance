# 参与开发

## 本地环境

需要 Python 3.11 或更高版本。创建虚拟环境后安装开发依赖：

```bash
python -m pip install -e ".[dev]"
```

默认使用 `LLM_PROVIDER=mock` 和 `MARKET_DATA_PROVIDER=sample`。除非测试明确标记为 `online`，否则不得依赖外部网络、真实 API Key 或产生费用的模型调用。

## 提交前检查

```bash
python -m pyflakes src Scripts tests
python -m compileall -q src Scripts tests
python -m pytest -q -m "not online" -p no:cacheprovider
```

提交应保持单一目的，说明行为变化和验证结果。不要提交 `.env`、数据库、日志、缓存、部署压缩包或 Word 临时锁文件。

## Pull Request

Pull Request 应包含问题背景、修改范围、测试结果和已知限制。涉及金融计算、风控阈值或 Sharpe 指标时，必须保留原公式和基线，并提供可复现对照结果。
