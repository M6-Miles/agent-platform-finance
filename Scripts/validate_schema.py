"""
Scripts: validate_schema.py
自动化验收脚本 —— 校验 Agent 输出是否符合预定义 JSON Schema。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# 技术分析输出 Schema
TECHNICAL_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["symbol", "source", "updated_at", "latest_close",
                 "latest_ma5", "latest_ma20", "latest_rsi", "latest_macd"],
    "properties": {
        "symbol": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
        "latest_close": {"type": "number"},
        "latest_ma5": {"type": "number"},
        "latest_ma20": {"type": "number"},
        "latest_rsi": {"type": "number"},
        "latest_macd": {"type": "number"},
        "latest_bb_position_pct": {"type": "number"},
        "total_return_pct": {"type": "number"},
    },
}

# 综合研判输出 Schema
SYNTHESIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["symbol", "signal", "confidence", "reasoning", "source", "updated_at"],
    "properties": {
        "symbol": {"type": "string"},
        "signal": {"type": "string", "enum": ["buy", "sell", "hold", "watch"]},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "target_price_low": {"type": "number"},
        "target_price_high": {"type": "number"},
        "reasoning": {"type": "string"},
        "source": {"type": "string"},
        "updated_at": {"type": "string"},
    },
}

SCHEMAS = {
    "technical": TECHNICAL_ANALYSIS_SCHEMA,
    "synthesis": SYNTHESIS_SCHEMA,
}


def validate(data: dict[str, Any], schema_name: str) -> list[str]:
    """返回验证错误列表；空列表表示通过。"""
    try:
        import jsonschema
        schema = SCHEMAS[schema_name]
        validator = jsonschema.Draft7Validator(schema)
        return [e.message for e in validator.iter_errors(data)]
    except ImportError:
        # jsonschema 未安装时退化为必填字段检查
        schema = SCHEMAS[schema_name]
        errors = []
        for field in schema.get("required", []):
            if field not in data:
                errors.append(f"缺少必填字段: {field}")
        return errors


if __name__ == "__main__":
    # 用法: python Scripts/validate_schema.py <json_file> <schema_name>
    if len(sys.argv) < 3:
        print("用法: python validate_schema.py <json_file> <technical|synthesis>")
        sys.exit(1)

    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    errors = validate(data, sys.argv[2])
    if errors:
        print(f"❌ Schema 校验失败 ({len(errors)} 个错误):")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    print("✅ Schema 校验通过")
