"""验证前端-后端字段映射契约"""
import re
from pathlib import Path

def extract_frontend_field_usage(html_path: Path) -> dict[str, list[str]]:
    """从前端 HTML 提取各 Agent 使用的字段"""
    html = html_path.read_text(encoding='utf-8')

    # 提取 renderFundamentalAnalysis 函数
    fund_match = re.search(
        r'function renderFundamentalAnalysis\(data\)\s*{([^}]+(?:{[^}]*}[^}]*)*)}',
        html,
        re.DOTALL
    )
    fund_fields = []
    if fund_match:
        fund_body = fund_match.group(1)
        fund_fields = re.findall(r'data\.(\w+)', fund_body)

    # 提取 renderIndustryAnalysis 函数
    ind_match = re.search(
        r'function renderIndustryAnalysis\(data\)\s*{([^}]+(?:{[^}]*}[^}]*)*)}',
        html,
        re.DOTALL
    )
    ind_fields = []
    if ind_match:
        ind_body = ind_match.group(1)
        ind_fields = re.findall(r'data\.(\w+)', ind_body)

    # 提取 renderMarketRegime 函数
    mr_match = re.search(
        r'function renderMarketRegime\(data\)\s*{([^}]+(?:{[^}]*}[^}]*)*)}',
        html,
        re.DOTALL
    )
    mr_fields = []
    if mr_match:
        mr_body = mr_match.group(1)
        mr_fields = re.findall(r'data\.(\w+)', mr_body)

    return {
        'fundamental': list(set(fund_fields)),
        'industry': list(set(ind_fields)),
        'market_regime': list(set(mr_fields)),
    }


def get_backend_schema() -> dict[str, list[str]]:
    """定义后端实际返回的字段（从 to_dict() 方法）"""
    return {
        'fundamental': [
            'symbol', 'name', 'source', 'updated_at',
            'pe_ttm', 'pb', 'total_market_value_cny', 'roe_pct',
            'valuation_signal', 'valuation_note', 'disclaimer', '_markdown',
            'data_status', 'fallback_reason', 'field_status',
        ],
        'industry': [
            'symbol', 'industry_name', 'source', 'updated_at',
            'prosperity_signal', 'prosperity_note', 'top_stocks',
            'fund_flow_3d_cny', 'disclaimer', '_markdown',
            'data_status', 'fallback_reason',
        ],
        'market_regime': [
            'regime', 'risk_appetite', 'index_code', 'index_close',
            'index_change_pct_5d', 'northbound_flow_cny',
            'regime_note', 'source', 'updated_at', 'disclaimer', '_markdown',
            'data_status', 'fallback_reason',
        ],
    }


def verify_contract():
    """验证前端使用的字段与后端 schema 一致"""
    project_root = Path(__file__).parent.parent
    html_path = project_root / 'frontend_prototype.html'

    if not html_path.exists():
        print(f"ERROR: {html_path} not found")
        return False

    frontend_fields = extract_frontend_field_usage(html_path)
    backend_schema = get_backend_schema()

    all_pass = True

    for agent, fe_fields in frontend_fields.items():
        be_fields = backend_schema[agent]
        print(f"\n=== {agent.upper()} Agent ===")
        print(f"Frontend uses: {sorted(fe_fields)}")
        print(f"Backend provides: {sorted(be_fields)}")

        # 检查前端使用的字段是否都在后端 schema 中
        missing = [f for f in fe_fields if f not in be_fields]
        if missing:
            print(f"  ERROR: Frontend uses fields not in backend: {missing}")
            all_pass = False
        else:
            print("  OK: All frontend fields exist in backend schema")

    return all_pass


if __name__ == '__main__':
    import sys
    success = verify_contract()
    sys.exit(0 if success else 1)
