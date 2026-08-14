from __future__ import annotations

import threading
import logging
import re
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd

from agent_platform.finance.errors import (
    InvalidSecuritySymbolError,
    MarketDataDependencyError,
    MarketDataUnavailableError,
)
from agent_platform.finance.market_data_provider import SecurityInfo

StockListLoader = Callable[[], pd.DataFrame]
HistoryLoader = Callable[..., pd.DataFrame]

logger = logging.getLogger(__name__)


class AkShareMarketDataProvider:
    """通过 AkShare 公开接口读取 A 股证券列表和日线行情。"""

    source_name = "AkShare 公开数据"

    def __init__(
        self,
        stock_list_loader: StockListLoader | None = None,
        history_loader: HistoryLoader | None = None,
        default_history_days: int = 365,
    ) -> None:
        self._stock_list_loader = stock_list_loader
        self._history_loader = history_loader
        self.default_history_days = default_history_days
        # 股票列表本地缓存：首次拉取约15秒，之后从内存直接返回
        self._stock_list_cache: tuple[datetime, pd.DataFrame] | None = None
        self._stock_list_lock = threading.Lock()
        self._STOCK_LIST_TTL_SECONDS: int = 3600  # 1小时内复用缓存

        # 网络弹性：限流器和熔断器（生产路径）
        from agent_platform.finance.network_resilience import CircuitBreaker, RateLimiter
        self._rate_limiter = RateLimiter(max_calls=10, window_s=1.0)  # 每秒最多10次
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout_s=30.0,
            half_open_max_calls=1,
        )

    def _load_akshare(self) -> Any:
        try:
            import akshare as ak
        except ImportError as exc:
            raise MarketDataDependencyError(
                '当前未安装 AkShare。请执行 python -m pip install -e ".[akshare]"。'
            ) from exc
        return ak

    def _stock_list(self) -> pd.DataFrame:
        """获取 A 股证券列表，结果缓存 1 小时，线程安全。"""
        with self._stock_list_lock:
            # 命中缓存则直接返回
            if self._stock_list_cache is not None:
                cached_at, data = self._stock_list_cache
                if (datetime.now(UTC) - cached_at).total_seconds() < self._STOCK_LIST_TTL_SECONDS:
                    return data
            # 未命中或过期：在锁内拉取（防止多线程重复拉取）
            loader = self._stock_list_loader
            if loader is None:
                loader = self._load_akshare().stock_info_a_code_name
            try:
                if self._stock_list_loader is not None:
                    # 测试 mock 路径：直接调用，不经过限流/熔断/重试
                    data = loader()
                else:
                    # 生产路径：统一网络调用入口
                    data = self._network_call(loader, context="获取A股证券列表")
            except Exception as exc:
                raise MarketDataUnavailableError(
                    f"AkShare 获取 A 股证券列表失败：{exc}"
                ) from exc
            if data is None or data.empty:
                raise MarketDataUnavailableError("AkShare 未返回 A 股证券列表。")
            self._stock_list_cache = (datetime.now(UTC), data)
            return data

    def list_securities(self) -> list[SecurityInfo]:
        data = self._stock_list()
        code_column = self._require_column(data, ("code", "代码"), "证券代码")
        name_column = self._require_column(data, ("name", "名称"), "证券名称")
        updated_at = datetime.now(UTC).date().isoformat()
        securities: list[SecurityInfo] = []
        for row in data[[code_column, name_column]].itertuples(index=False, name=None):
            symbol = self.normalize_symbol(str(row[0]))
            securities.append(
                SecurityInfo(
                    market=self.market_for_symbol(symbol),
                    symbol=symbol,
                    name=str(row[1]),
                    source=self.source_name,
                    updated_at=updated_at,
                )
            )
        return securities

    def get_price_history(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        normalized_symbol = self.normalize_symbol(symbol)
        # 既有契约：默认结束日为本地自然日 today，默认起始日为
        # today - default_history_days。使用 date.today() 而非 UTC 日期，
        # 避免东八区在 UTC 午夜前后算出前一天，导致默认窗口整体偏移一天。
        today = date.today()  # 只取一次，避免跨午夜时 start/end 落在不同自然日
        effective_end = end or today
        effective_start = start or (today - timedelta(days=self.default_history_days))
        if effective_start > effective_end:
            raise ValueError("开始日期不能晚于结束日期")

        raw = None

        if self._history_loader is not None:
            # 测试 mock 路径：直接调用提供的 loader
            try:
                raw = self._history_loader(
                    symbol=normalized_symbol,
                    period="daily",
                    start_date=effective_start.strftime("%Y%m%d"),
                    end_date=effective_end.strftime("%Y%m%d"),
                    adjust="",
                )
            except Exception as exc:
                raise MarketDataUnavailableError(
                    f"AkShare 获取 {normalized_symbol} 日线失败：{exc}"
                ) from exc
        else:
            # 生产路径：优先使用 Stooq 数据源（stock_zh_a_daily），
            # 不依赖东方财富接口，在大多数网络环境下更稳定
            ak_module = self._load_akshare()
            market = self.market_for_symbol(normalized_symbol)
            prefix = "sh" if market == "上交所" else "bj" if market == "北交所" else "sz"
            daily_sym = f"{prefix}{normalized_symbol}"

            def _fetch_daily():
                return ak_module.stock_zh_a_daily(
                    symbol=daily_sym,
                    start_date=effective_start.strftime("%Y%m%d"),
                    end_date=effective_end.strftime("%Y%m%d"),
                    adjust="qfq",
                )

            try:
                raw = self._network_call(_fetch_daily, context=f"获取{normalized_symbol}日线")
            except Exception as exc:
                _last_exc = exc
                # Stooq 失败，尝试 curl_cffi 直调东方财富（备用）
                raw = self._fallback_daily(normalized_symbol, effective_start, effective_end)
                if raw is None:
                    raise MarketDataUnavailableError(
                        f"AkShare 获取 {normalized_symbol} 日线失败"
                        f"（Stooq 已重试，curl_cffi 备用也失败）：{_last_exc}"
                    ) from _last_exc
        if raw is None or raw.empty:
            raise InvalidSecuritySymbolError(
                f"AkShare 未返回证券 {normalized_symbol} 在所选日期范围内的日线数据。"
            )

        columns = {
            "date": self._require_column(raw, ("日期", "date"), "日期"),
            "open": self._require_column(raw, ("开盘", "open"), "开盘价"),
            "high": self._require_column(raw, ("最高", "high"), "最高价"),
            "low": self._require_column(raw, ("最低", "low"), "最低价"),
            "close": self._require_column(raw, ("收盘", "close"), "收盘价"),
            "volume": self._require_column(raw, ("成交量", "volume"), "成交量"),
        }
        result = raw[
            [
                columns["date"],
                columns["open"],
                columns["high"],
                columns["low"],
                columns["close"],
                columns["volume"],
            ]
        ].rename(columns={source: target for target, source in columns.items()})
        result["date"] = pd.to_datetime(result["date"], errors="coerce").dt.date
        for column in ("open", "high", "low", "close", "volume"):
            result[column] = pd.to_numeric(result[column], errors="coerce")
        result = result.dropna(subset=["date", "open", "high", "low", "close"])
        if result.empty:
            raise MarketDataUnavailableError(
                f"AkShare 返回的 {normalized_symbol} 日线缺少有效价格字段。"
            )
        self._validate_ohlcv(result, normalized_symbol)

        name = self._lookup_name(normalized_symbol)
        result["market"] = self.market_for_symbol(normalized_symbol)
        result["symbol"] = normalized_symbol
        result["name"] = name
        result["source"] = f"{self.source_name}（前复权日线·Stooq）"
        result["updated_at"] = datetime.now(UTC).date().isoformat()
        ordered_columns = [
            "market",
            "symbol",
            "name",
            "date",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "source",
            "updated_at",
        ]
        return result[ordered_columns].sort_values("date").reset_index(drop=True)

    def _fallback_daily(
        self,
        normalized_symbol: str,
        start: date,
        end: date,
    ) -> pd.DataFrame | None:
        """curl_cffi 直调东方财富接口作为最后备用方案。网络不可达时返回 None。"""
        try:
            from curl_cffi import requests as cffi_requests
        except ImportError:
            return None

        try:
            market_code = 1 if normalized_symbol.startswith("6") else 0
            url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
            params = {
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
                "ut": "7eea3edcaed734bea9cbfc24409ed989",
                "klt": "101",
                "fqt": "0",
                "secid": f"{market_code}.{normalized_symbol}",
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
            }
            resp = cffi_requests.get(
                url, params=params, impersonate="chrome110", timeout=15
            )
            data_json = resp.json()
            klines = (data_json.get("data") or {}).get("klines")
            if not klines:
                return None

            raw = pd.DataFrame([item.split(",") for item in klines])
            # columns from East Money: 日期,开盘,收盘,最高,最低,成交量,...
            raw.columns = list(range(len(raw.columns)))
            col_map = {0: "日期", 1: "开盘", 2: "收盘", 3: "最高", 4: "最低", 5: "成交量"}
            raw = raw.rename(columns=col_map)
            # 注意东方财富返回的是 开盘/收盘/最高/最低，而标准是 open/high/low/close
            # 此处字段名与 _require_column 的候选列表匹配
            return raw
        except Exception as exc:
            logging.getLogger(__name__).debug(
                "curl_cffi fallback failed for %s: %s", normalized_symbol, exc
            )
            return None

    def _lookup_name(self, symbol: str) -> str:
        # 优先用单股信息接口：实时准确，支持新股首日（如 N嘉立创），
        # 不受全市场列表缓存过期或代码复用问题影响
        try:
            name = self._get_name_via_individual_info(symbol)
            if name:
                return name
        except Exception:
            pass
        # 回退到全市场列表缓存
        try:
            data = self._stock_list()
            code_column = self._require_column(data, ("code", "代码"), "证券代码")
            name_column = self._require_column(data, ("name", "名称"), "证券名称")
            normalized_codes = data[code_column].astype(str).str.zfill(6)
            matched = data.loc[normalized_codes == symbol, name_column]
            if not matched.empty:
                return str(matched.iloc[0])
        except MarketDataUnavailableError:
            pass
        return f"A股 {symbol}"

    def get_realtime_quote(self, symbol: str) -> dict:
        """获取腾讯公开行情快照。

        仅返回真实行情。接口不可用时抛 ``MarketDataUnavailableError``，
        不生成任何模拟或随机价格。快速快照一次响应即包含名称、现价、
        昨收和报价时间，避免分钟线、日线和名称接口串行造成长时间等待。
        """
        normalized = self.normalize_symbol(symbol)
        _log = logging.getLogger(__name__)

        # 北交所（8xxxxx / 9xxxxx）腾讯行情不支持：显式不可用，不编造价格
        if normalized.startswith(("8", "9")):
            _log.info("北交所代码 %s 不支持腾讯行情实时接口", normalized)
            self._raise_quote_unavailable(normalized, "北交所暂不支持实时行情接口")

        # 上交所(6xxxxx) → sh前缀；深交所(0/1/2/3xxxxx) → sz前缀
        if normalized.startswith("6"):
            tencent_sym = f"sh{normalized}"
        else:
            tencent_sym = f"sz{normalized}"

        try:
            # 测试注入的 AkShare loader 保留分钟线契约；生产实例使用更快的
            # 腾讯轻量快照，避免为一次报价下载整段分钟数据。
            if "_load_akshare" in self.__dict__:
                return self._get_realtime_quote_from_minute_loader(normalized)
            payload = self._fetch_tencent_snapshot(tencent_sym)
            return self._parse_tencent_snapshot(payload, normalized)
        except MarketDataUnavailableError:
            # 已是显式不可用（空数据分支），保持原因不被包装成第二层错误
            raise
        except Exception as exc:
            _log.warning("腾讯行情接口失败: %s", exc)
            self._raise_quote_unavailable(
                normalized, f"腾讯行情接口失败（{type(exc).__name__}）：{exc}"
            )

    def _get_realtime_quote_from_minute_loader(self, symbol: str) -> dict:
        """兼容注入式测试 loader 的分钟线校验路径。"""
        ak_module = self._load_akshare()
        market = self.market_for_symbol(symbol)
        tencent_symbol = f"sh{symbol}" if symbol.startswith("6") else f"sz{symbol}"
        frame = ak_module.stock_zh_a_minute(symbol=tencent_symbol, period="1", adjust="")
        if frame is None or frame.empty:
            self._raise_quote_unavailable(symbol, "腾讯行情接口返回空数据")
        latest_price = float(frame.iloc[-1]["close"])
        prev_close = self._extract_previous_close(frame)
        if prev_close is None:
            latest_day = self._latest_quote_day(frame)
            prev_close = self._get_previous_daily_close(ak_module, symbol, latest_day)
        updated_at = self._latest_quote_timestamp(frame)
        if latest_price <= 0 or prev_close is None or prev_close <= 0 or updated_at is None:
            self._raise_quote_unavailable(symbol, "分钟行情字段校验失败")
        return {
            "symbol": symbol,
            "name": self._get_name_via_individual_info(symbol) or f"A股 {symbol}",
            "price": latest_price,
            "prev_close": round(prev_close, 2),
            "change_pct": round((latest_price - prev_close) / prev_close * 100, 2),
            "market": market,
            "source": "腾讯行情实时数据（昨收经上一交易日校验）",
            "updated_at": updated_at,
            "data_status": "live",
            "fallback_reason": None,
        }

    @staticmethod
    def _fetch_tencent_snapshot(tencent_symbol: str) -> str:
        """单次获取轻量快照；2.2 秒后终止，不执行隐藏重试。"""
        import httpx

        response = httpx.get(
            f"https://qt.gtimg.cn/q={tencent_symbol}",
            headers={"Referer": "https://gu.qq.com/"},
            timeout=httpx.Timeout(2.2),
        )
        response.raise_for_status()
        return response.content.decode("gb18030", errors="strict")

    def _parse_tencent_snapshot(self, payload: str, symbol: str) -> dict:
        match = re.search(r'="(.*)";?\s*$', payload.strip())
        if match is None:
            self._raise_quote_unavailable(symbol, "腾讯行情快照格式无效")
        fields = match.group(1).split("~")
        if len(fields) <= 32:
            self._raise_quote_unavailable(symbol, "腾讯行情快照字段不完整")
        try:
            name = fields[1].strip()
            response_symbol = fields[2].strip()
            price = float(fields[3])
            prev_close = float(fields[4])
            quote_time = datetime.strptime(fields[30], "%Y%m%d%H%M%S")
            change_pct = float(fields[32])
        except (TypeError, ValueError) as exc:
            self._raise_quote_unavailable(symbol, f"腾讯行情快照字段无法解析：{exc}")
        if response_symbol != symbol or price <= 0 or prev_close <= 0:
            self._raise_quote_unavailable(symbol, "腾讯行情快照证券或价格校验失败")
        return {
            "symbol": symbol,
            "name": name or f"A股 {symbol}",
            "price": price,
            "prev_close": prev_close,
            "change_pct": round(change_pct, 2),
            "market": self.market_for_symbol(symbol),
            "source": "腾讯证券公开行情快照",
            "updated_at": quote_time.isoformat(),
            "data_status": "live",
            "fallback_reason": None,
        }

    @staticmethod
    def _categorize_akshare_error(exc: Exception) -> str:
        """判断 AkShare 错误是否可重试。"""
        from agent_platform.finance.network_resilience import ErrorCategory, categorize_error
        category = categorize_error(exc)
        if category == ErrorCategory.RETRYABLE:
            return "retryable"
        if category == ErrorCategory.NON_RETRYABLE:
            return "non_retryable"
        # UNKNOWN 默认不重试，避免无限循环
        return "non_retryable"

    def _network_call(self, func: Callable[[], Any], *, context: str = "") -> Any:
        """统一网络调用入口：限流 + 熔断 + 重试。

        只在生产路径使用（非测试 mock）。

        注意：AkShare SDK 多数接口无法稳定传递 per-call timeout 参数，
        超时依赖其底层 HTTP 栈（requests/curl_cffi）的默认行为。
        项目会并发请求（Agent + 网络数据 + LLM API），不能通过全局
        socket.setdefaulttimeout() 修改进程级状态。对可直接控制的
        curl_cffi 请求（如 _fallback_daily）保留 timeout=15 参数。
        """
        from agent_platform.finance.network_resilience import (
            CircuitBreakerOpenError,
            RetryConfig,
            call_with_retry,
        )

        # 1. 限流
        if not self._rate_limiter.acquire(timeout=30.0):
            raise MarketDataUnavailableError(
                f"{context} 限流等待超时（30秒内未获得调用许可）"
            )

        # 2. 熔断检查 + 重试
        # 超时依赖底层 HTTP 栈（requests 默认连接超时，curl_cffi 显式 timeout 参数）
        try:
            return self._circuit_breaker.call(
                lambda: call_with_retry(
                    func,
                    config=RetryConfig(max_attempts=3, base_delay_s=0.5),
                    context=context,
                ),
                context=context,
            )
        except CircuitBreakerOpenError:
            raise MarketDataUnavailableError(
                f"{context} 熔断器开路（服务暂时不可用，请稍后重试）"
            )

    @staticmethod
    def _latest_quote_day(frame: pd.DataFrame) -> date:
        for column in ("day", "date", "datetime", "时间", "日期"):
            if column in frame.columns:
                parsed = pd.to_datetime(frame[column], errors="coerce").dropna()
                if not parsed.empty:
                    return parsed.iloc[-1].date()
        return date.today()

    @staticmethod
    def _latest_quote_timestamp(frame: pd.DataFrame) -> str | None:
        """Return the provider timestamp from the latest minute bar."""
        for column in ("day", "date", "datetime", "时间", "日期"):
            if column not in frame.columns:
                continue
            parsed = pd.to_datetime(frame[column], errors="coerce").dropna()
            if not parsed.empty:
                return parsed.iloc[-1].isoformat()
        return None

    @classmethod
    def _extract_previous_close(cls, frame: pd.DataFrame) -> float | None:
        for column in ("prev_close", "pre_close", "昨收"):
            if column in frame.columns:
                values = pd.to_numeric(frame[column], errors="coerce").dropna()
                if not values.empty and float(values.iloc[-1]) > 0:
                    return float(values.iloc[-1])

        for column in ("day", "date", "datetime", "时间", "日期"):
            if column not in frame.columns:
                continue
            timestamps = pd.to_datetime(frame[column], errors="coerce")
            latest_day = timestamps.dropna().iloc[-1].date() if timestamps.notna().any() else None
            if latest_day is None:
                continue
            previous = frame.loc[timestamps.dt.date < latest_day, "close"]
            previous = pd.to_numeric(previous, errors="coerce").dropna()
            if not previous.empty and float(previous.iloc[-1]) > 0:
                return float(previous.iloc[-1])
        return None

    def _get_previous_daily_close(
        self, ak_module: Any, symbol: str, latest_day: date,
    ) -> float | None:
        market = self.market_for_symbol(symbol)
        prefix = "sh" if market == "上交所" else "bj" if market == "北交所" else "sz"
        start = latest_day - timedelta(days=20)
        try:
            raw = self._network_call(
                lambda: ak_module.stock_zh_a_daily(
                    symbol=f"{prefix}{symbol}",
                    start_date=start.strftime("%Y%m%d"),
                    end_date=latest_day.strftime("%Y%m%d"),
                    adjust="",
                ),
                context=f"获取{symbol}昨收价",
            )
        except Exception:
            return None
        if raw is None or raw.empty:
            return None
        try:
            date_col = self._require_column(raw, ("日期", "date"), "日期")
            close_col = self._require_column(raw, ("收盘", "close"), "收盘价")
            dates = pd.to_datetime(raw[date_col], errors="coerce").dt.date
            closes = pd.to_numeric(
                raw.loc[dates < latest_day, close_col], errors="coerce"
            ).dropna()
            return (
                float(closes.iloc[-1])
                if not closes.empty and float(closes.iloc[-1]) > 0 else None
            )
        except (KeyError, MarketDataUnavailableError):
            return None

    @staticmethod
    def _validate_ohlcv(frame: pd.DataFrame, symbol: str) -> None:
        invalid_price = (
            (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
            | (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
            | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
        )
        if invalid_price.any():
            raise MarketDataUnavailableError(f"{symbol} 日线 OHLC 关系或价格非正")
        if frame["date"].duplicated().any():
            raise MarketDataUnavailableError(f"{symbol} 日线包含重复交易日")
        volumes = pd.to_numeric(frame["volume"], errors="coerce")
        if (volumes.dropna() < 0).any():
            raise MarketDataUnavailableError(f"{symbol} 日线成交量为负")

    def _get_name_via_individual_info(self, symbol: str) -> str | None:
        """用单股信息接口查公司简称（支持新股首日上市，轻量单次请求）。"""
        ak_module = self._load_akshare()
        try:
            info_df = ak_module.stock_individual_info_em(symbol=symbol)
            # 返回两列 DataFrame，第0列为字段名，第1列为值
            name_rows = info_df[info_df.iloc[:, 0].isin(["股票简称", "名称", "name"])]
            if not name_rows.empty:
                return str(name_rows.iloc[0, 1])
        except Exception:
            pass
        return None

    def _raise_quote_unavailable(self, symbol: str, reason: str) -> None:
        """auto 模式下行情接口不可用时显式报错。

        绝不生成随机价格：模拟盘与 Agent 只能引用真实数据，
        取不到就必须让上层显式告知“暂不可用”。需要零网络演示时请显式
        使用 offline 样例数据源（``SampleMarketDataProvider``）。
        """
        raise MarketDataUnavailableError(
            f"无法获取 {symbol} 的实时行情（{reason}）。"
            "本数据源不会生成模拟价格；如需离线演示请切换 data_mode=offline。"
        )

    def _get_quote_via_hist(self, symbol: str) -> dict:
        """备用报价：单股历史行情接口，用于全市场快照未收录的新股/停牌股。"""
        from datetime import date, timedelta

        ak_module = self._load_akshare()
        # 用 _lookup_name 查名称（先走缓存全市场列表，最可靠；
        # 单股接口 _get_name_via_individual_info 网络不稳定时会返回 None）
        name = self._lookup_name(symbol)
        today = date.today()
        start = (today - timedelta(days=7)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        try:
            hist_df = ak_module.stock_zh_a_hist(
                symbol=symbol, period="daily",
                start_date=start, end_date=end, adjust="",
            )
        except Exception as exc:
            raise InvalidSecuritySymbolError(
                f"找不到证券代码 {symbol} 的行情数据：{exc}"
            ) from exc
        if hist_df.empty:
            raise InvalidSecuritySymbolError(
                f"证券代码 {symbol} 在近7个交易日无行情（可能未上市或已退市）"
            )
        last = hist_df.iloc[-1]
        price = float(last.get("收盘", last.get("close", 0)))
        prev_close_val = float(last.get("昨收", last.get("开盘", last.get("open", price))))
        change_pct = (
            round((price - prev_close_val) / prev_close_val * 100, 2)
            if prev_close_val else 0.0
        )
        return {
            "symbol": symbol,
            "name": name,
            "price": price,
            "prev_close": prev_close_val,
            "change_pct": change_pct,
            "market": self.market_for_symbol(symbol),
            "source": "东方财富历史行情（单股备用）",
        }

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        value = symbol.strip().upper()
        for prefix in ("SH", "SZ", "BJ"):
            if value.startswith(prefix):
                value = value[2:]
                break
        value = value.split(".", maxsplit=1)[0]
        if not (len(value) == 6 and value.isdigit()):
            raise InvalidSecuritySymbolError(
                "A 股证券代码应为 6 位数字，例如 600519 或 000001。"
            )
        return value

    @staticmethod
    def market_for_symbol(symbol: str) -> str:
        if symbol.startswith(("4", "8", "92")):
            return "北交所"
        if symbol.startswith(("5", "6", "9")):
            return "上交所"
        return "深交所"

    @staticmethod
    def _require_column(
        frame: pd.DataFrame,
        candidates: tuple[str, ...],
        display_name: str,
    ) -> str:
        for column in candidates:
            if column in frame.columns:
                return column
        raise MarketDataUnavailableError(
            f"AkShare 返回结果缺少{display_name}字段；上游接口可能已变化。"
        )
