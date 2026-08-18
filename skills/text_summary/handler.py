"""最小 Skill 示例：仅使用本地字符串处理，不访问网络和业务数据库。"""


def run(text: str, max_length: int = 120) -> dict:
    text = " ".join(str(text).split())
    max_length = max(20, min(int(max_length), 500))
    return {
        "summary": text if len(text) <= max_length else text[: max_length - 1] + "…",
        "source": "local_skill/text_summary",
    }
