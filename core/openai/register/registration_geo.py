# -*- coding: utf-8 -*-
"""
注册地域硬门禁的纯校验层。

职责：按画像国家（缺省时区时使用该国家的确定默认时区）生成并验证一份内部相容的
冻结语言/时区画像，并给出可对外分类的错误。这里不持有 HTTP 会话，也不访问任何
代理/凭据；网络探测与证据聚合由 core.openai.transport.session.BrowserSession 完成。

安全约束：所有错误文本只保留来源标签、阶段、状态与国家/时区摘要，
绝不包含代理 URL、认证信息、响应内容或任何凭据。
"""
from __future__ import annotations

import logging
from datetime import datetime
from ipaddress import ip_address
from zoneinfo import ZoneInfo


logger = logging.getLogger(__name__)


# ---- 对外可判定的失败类别（进入任务结果 category 字段）----
CATEGORY_PROXY = "proxy"                    # 代理连接/认证失败（有确凿代理证据）
CATEGORY_GEO_SERVICE = "geo_service"        # 地域服务不可达/非成功状态
CATEGORY_GEO_PARSE = "geo_parse"            # 地域响应解析失败
CATEGORY_GEO_INCOMPLETE = "geo_incomplete"  # 地域信息不完整（缺国家/时区/未知国家）
CATEGORY_GEO_MISMATCH = "geo_mismatch"      # 出口与画像不一致

# ---- 失败阶段（进入任务结果 stage 字段）----
STAGE_DETECT = "detect"          # 地域证据采集
STAGE_VALIDATE = "validate"      # 完整性/相容性校验
STAGE_FREEZE = "freeze"          # 首次冻结合格画像
STAGE_REVALIDATE = "revalidate"  # 换出口后复核冻结画像


class RegistrationGeoError(RuntimeError):
    """注册地域门禁失败；携带安全 category/stage 供任务结果透传。"""

    def __init__(self, message: str, category: str, stage: str):
        super().__init__(message)
        self.category = category
        self.stage = stage


# ---- 国家 → 合法 IANA 时区集合（含常见别名）----
# 覆盖本模块 COUNTRY_DEFAULT_TIMEZONE 与 _COUNTRY_LANGUAGE_TAG 全部国家，
# 保证「已知国家」与「可判时区相容」边界一致。
COUNTRY_TIMEZONES: dict[str, frozenset] = {
    "JP": frozenset({"Asia/Tokyo"}),
    "CN": frozenset({"Asia/Shanghai", "Asia/Urumqi"}),
    "HK": frozenset({"Asia/Hong_Kong"}),
    "TW": frozenset({"Asia/Taipei"}),
    "SG": frozenset({"Asia/Singapore"}),
    "VN": frozenset({"Asia/Ho_Chi_Minh", "Asia/Saigon"}),
    "KR": frozenset({"Asia/Seoul"}),
    "TH": frozenset({"Asia/Bangkok"}),
    "ID": frozenset({"Asia/Jakarta", "Asia/Pontianak", "Asia/Makassar", "Asia/Jayapura"}),
    "MY": frozenset({"Asia/Kuala_Lumpur", "Asia/Kuching"}),
    "PH": frozenset({"Asia/Manila"}),
    "IN": frozenset({"Asia/Kolkata", "Asia/Calcutta"}),
    "GB": frozenset({"Europe/London"}),
    "IE": frozenset({"Europe/Dublin"}),
    "DE": frozenset({"Europe/Berlin", "Europe/Busingen"}),
    "FR": frozenset({"Europe/Paris"}),
    "NL": frozenset({"Europe/Amsterdam"}),
    "ES": frozenset({"Europe/Madrid", "Africa/Ceuta", "Atlantic/Canary"}),
    "IT": frozenset({"Europe/Rome"}),
    "PT": frozenset({"Europe/Lisbon", "Atlantic/Azores", "Atlantic/Madeira"}),
    "PL": frozenset({"Europe/Warsaw"}),
    "AT": frozenset({"Europe/Vienna"}),
    "CH": frozenset({"Europe/Zurich"}),
    "BE": frozenset({"Europe/Brussels"}),
    "SE": frozenset({"Europe/Stockholm"}),
    "NO": frozenset({"Europe/Oslo"}),
    "DK": frozenset({"Europe/Copenhagen"}),
    "FI": frozenset({"Europe/Helsinki"}),
    "CZ": frozenset({"Europe/Prague"}),
    "RO": frozenset({"Europe/Bucharest"}),
    "HU": frozenset({"Europe/Budapest"}),
    "GR": frozenset({"Europe/Athens"}),
    "TR": frozenset({"Europe/Istanbul"}),
    "IL": frozenset({"Asia/Jerusalem"}),
    "UA": frozenset({"Europe/Kyiv", "Europe/Kiev", "Europe/Uzhgorod", "Europe/Zaporozhye"}),
    "AE": frozenset({"Asia/Dubai"}),
    "SA": frozenset({"Asia/Riyadh"}),
    "ZA": frozenset({"Africa/Johannesburg"}),
    "NZ": frozenset({"Pacific/Auckland", "Pacific/Chatham"}),
    "US": frozenset({
        "America/New_York", "America/Detroit", "America/Kentucky/Louisville",
        "America/Kentucky/Monticello", "America/Indiana/Indianapolis",
        "America/Indiana/Vincennes", "America/Indiana/Winamac",
        "America/Indiana/Marengo", "America/Indiana/Petersburg",
        "America/Indiana/Vevay", "America/Chicago", "America/Indiana/Tell_City",
        "America/Indiana/Knox", "America/Menominee", "America/North_Dakota/Center",
        "America/North_Dakota/New_Salem", "America/North_Dakota/Beulah",
        "America/Denver", "America/Boise", "America/Phoenix",
        "America/Los_Angeles", "America/Anchorage", "America/Juneau",
        "America/Sitka", "America/Metlakatla", "America/Yakutat",
        "America/Nome", "America/Adak", "Pacific/Honolulu",
        "US/Pacific", "US/Mountain", "US/Central", "US/Eastern",
        "US/Alaska", "US/Hawaii", "US/Arizona",
    }),
    "CA": frozenset({
        "America/St_Johns", "America/Halifax", "America/Glace_Bay",
        "America/Moncton", "America/Goose_Bay", "America/Blanc-Sablon",
        "America/Toronto", "America/Iqaluit", "America/Nipigon",
        "America/Thunder_Bay", "America/Pangnirtung", "America/Winnipeg",
        "America/Rainy_River", "America/Resolute", "America/Rankin_Inlet",
        "America/Regina", "America/Swift_Current", "America/Edmonton",
        "America/Cambridge_Bay", "America/Yellowknife", "America/Inuvik",
        "America/Creston", "America/Dawson_Creek", "America/Fort_Nelson",
        "America/Vancouver", "America/Whitehorse", "America/Dawson",
    }),
    "AU": frozenset({
        "Australia/Lord_Howe", "Antarctica/Macquarie", "Australia/Hobart",
        "Australia/Melbourne", "Australia/Sydney", "Australia/Broken_Hill",
        "Australia/Brisbane", "Australia/Lindeman", "Australia/Adelaide",
        "Australia/Darwin", "Australia/Perth", "Australia/Eucla",
        "Australia/ACT", "Australia/NSW", "Australia/Victoria",
        "Australia/Queensland", "Australia/South", "Australia/West",
        "Australia/North", "Australia/Tasmania",
    }),
    "MX": frozenset({
        "America/Mexico_City", "America/Cancun", "America/Merida",
        "America/Monterrey", "America/Matamoros", "America/Mazatlan",
        "America/Chihuahua", "America/Ojinaga", "America/Hermosillo",
        "America/Bahia_Banderas", "America/Tijuana",
    }),
    "BR": frozenset({
        "America/Noronha", "America/Belem", "America/Fortaleza",
        "America/Recife", "America/Araguaina", "America/Maceio",
        "America/Bahia", "America/Sao_Paulo", "America/Campo_Grande",
        "America/Cuiaba", "America/Santarem", "America/Porto_Velho",
        "America/Boa_Vista", "America/Manaus", "America/Eirunepe",
        "America/Rio_Branco",
    }),
    "RU": frozenset({
        "Europe/Kaliningrad", "Europe/Moscow", "Europe/Volgograd",
        "Europe/Samara", "Europe/Saratov", "Europe/Ulyanovsk", "Europe/Kirov",
        "Europe/Astrakhan", "Asia/Yekaterinburg", "Asia/Omsk", "Asia/Novosibirsk",
        "Asia/Barnaul", "Asia/Tomsk", "Asia/Novokuznetsk", "Asia/Krasnoyarsk",
        "Asia/Irkutsk", "Asia/Chita", "Asia/Yakutsk", "Asia/Vladivostok",
        "Asia/Khandyga", "Asia/Sakhalin", "Asia/Ust-Nera", "Asia/Magadan",
        "Asia/Srednekolymsk", "Asia/Kamchatka", "Asia/Anadyr",
    }),
}

# 国家 → 注册画像默认 IANA 时区。覆盖 COUNTRY_TIMEZONES 全部国家，每个国家一个
# 合法且确定的时区，多时区国家取最通用／人口最多的规范时区。缺省时区由此表决定，
# 不再依赖外部 GeoIP 时区，也不为多时区国家拒绝默认生成。
COUNTRY_DEFAULT_TIMEZONE: dict[str, str] = {
    "JP": "Asia/Tokyo",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "TW": "Asia/Taipei",
    "SG": "Asia/Singapore",
    "VN": "Asia/Ho_Chi_Minh",
    "KR": "Asia/Seoul",
    "TH": "Asia/Bangkok",
    "ID": "Asia/Jakarta",
    "MY": "Asia/Kuala_Lumpur",
    "PH": "Asia/Manila",
    "IN": "Asia/Kolkata",
    "GB": "Europe/London",
    "IE": "Europe/Dublin",
    "DE": "Europe/Berlin",
    "FR": "Europe/Paris",
    "NL": "Europe/Amsterdam",
    "ES": "Europe/Madrid",
    "IT": "Europe/Rome",
    "PT": "Europe/Lisbon",
    "PL": "Europe/Warsaw",
    "AT": "Europe/Vienna",
    "CH": "Europe/Zurich",
    "BE": "Europe/Brussels",
    "SE": "Europe/Stockholm",
    "NO": "Europe/Oslo",
    "DK": "Europe/Copenhagen",
    "FI": "Europe/Helsinki",
    "CZ": "Europe/Prague",
    "RO": "Europe/Bucharest",
    "HU": "Europe/Budapest",
    "GR": "Europe/Athens",
    "TR": "Europe/Istanbul",
    "IL": "Asia/Jerusalem",
    "UA": "Europe/Kyiv",
    "AE": "Asia/Dubai",
    "SA": "Asia/Riyadh",
    "ZA": "Africa/Johannesburg",
    "NZ": "Pacific/Auckland",
    "US": "America/New_York",
    "CA": "America/Toronto",
    "AU": "Australia/Sydney",
    "MX": "America/Mexico_City",
    "BR": "America/Sao_Paulo",
    "RU": "Europe/Moscow",
}

# 国家 → 注册画像语言标签。与 COUNTRY_DEFAULT_TIMEZONE 一一对应，保证每个已知国家
# 都能生成完整语言与 IANA 时区。AU/CA 使用本国语言，不复用 GB 的 en-GB 或 US 的 en-US。
_COUNTRY_LANGUAGE_TAG: dict[str, str] = {
    "JP": "ja-JP", "CN": "zh-CN", "HK": "zh-HK", "TW": "zh-TW",
    "SG": "en-SG", "VN": "vi-VN", "KR": "ko-KR", "TH": "th-TH",
    "ID": "id-ID", "MY": "ms-MY", "PH": "en-PH", "IN": "en-IN",
    "GB": "en-GB", "IE": "en-IE", "DE": "de-DE", "FR": "fr-FR",
    "NL": "nl-NL", "ES": "es-ES", "IT": "it-IT", "PT": "pt-PT",
    "PL": "pl-PL", "AT": "de-AT", "CH": "de-CH", "BE": "nl-BE",
    "SE": "sv-SE", "NO": "nb-NO", "DK": "da-DK", "FI": "fi-FI",
    "CZ": "cs-CZ", "RO": "ro-RO", "HU": "hu-HU", "GR": "el-GR",
    "TR": "tr-TR", "IL": "he-IL", "UA": "uk-UA", "AE": "ar-AE",
    "SA": "ar-SA", "ZA": "en-ZA", "NZ": "en-NZ", "US": "en-US",
    "CA": "en-CA", "AU": "en-AU", "MX": "es-MX", "BR": "pt-BR",
    "RU": "ru-RU",
}

# 合法 IANA 别名 → 规范名（仅用于相容性判定；冻结画像保留观测原文拼写）。
_IANA_ALIASES: dict[str, str] = {
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Calcutta": "Asia/Kolkata",
    "Europe/Kiev": "Europe/Kyiv",
    "US/Pacific": "America/Los_Angeles",
    "US/Mountain": "America/Denver",
    "US/Central": "America/Chicago",
    "US/Eastern": "America/New_York",
    "US/Alaska": "America/Anchorage",
    "US/Hawaii": "Pacific/Honolulu",
    "US/Arizona": "America/Phoenix",
    "Australia/ACT": "Australia/Sydney",
    "Australia/NSW": "Australia/Sydney",
    "Australia/Victoria": "Australia/Melbourne",
    "Australia/Queensland": "Australia/Brisbane",
    "Australia/South": "Australia/Adelaide",
    "Australia/West": "Australia/Perth",
    "Australia/North": "Australia/Darwin",
    "Australia/Tasmania": "Australia/Hobart",
}

# 规范时区 → (标准时间长名, 夏令时长名)。无 DST 地区两者相同。
_TZ_DST_LONG_NAMES: dict[str, tuple[str, str]] = {
    # 亚洲 / 中东 / 非洲（无 DST）
    "Asia/Tokyo": ("Japan Standard Time", "Japan Standard Time"),
    "Asia/Shanghai": ("China Standard Time", "China Standard Time"),
    "Asia/Urumqi": ("Urumqi Time", "Urumqi Time"),
    "Asia/Hong_Kong": ("Hong Kong Standard Time", "Hong Kong Standard Time"),
    "Asia/Taipei": ("Taipei Standard Time", "Taipei Standard Time"),
    "Asia/Singapore": ("Singapore Standard Time", "Singapore Standard Time"),
    "Asia/Seoul": ("Korean Standard Time", "Korean Standard Time"),
    "Asia/Bangkok": ("Indochina Time", "Indochina Time"),
    "Asia/Ho_Chi_Minh": ("Indochina Time", "Indochina Time"),
    "Asia/Jakarta": ("Western Indonesia Time", "Western Indonesia Time"),
    "Asia/Pontianak": ("Western Indonesia Time", "Western Indonesia Time"),
    "Asia/Makassar": ("Central Indonesia Time", "Central Indonesia Time"),
    "Asia/Jayapura": ("Eastern Indonesia Time", "Eastern Indonesia Time"),
    "Asia/Kuala_Lumpur": ("Malaysia Time", "Malaysia Time"),
    "Asia/Kuching": ("Malaysia Time", "Malaysia Time"),
    "Asia/Manila": ("Philippine Standard Time", "Philippine Standard Time"),
    "Asia/Kolkata": ("India Standard Time", "India Standard Time"),
    "Asia/Dubai": ("Gulf Standard Time", "Gulf Standard Time"),
    "Asia/Riyadh": ("Arabian Standard Time", "Arabian Standard Time"),
    "Asia/Jerusalem": ("Israel Standard Time", "Israel Daylight Time"),
    "Africa/Johannesburg": ("South Africa Standard Time", "South Africa Standard Time"),
    # 欧洲（含 DST）
    "Europe/London": ("Greenwich Mean Time", "British Summer Time"),
    "Europe/Dublin": ("Greenwich Mean Time", "Irish Standard Time"),
    "Europe/Berlin": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Paris": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Amsterdam": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Madrid": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Rome": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Warsaw": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Vienna": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Zurich": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Brussels": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Stockholm": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Oslo": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Copenhagen": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Prague": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Budapest": ("Central European Standard Time", "Central European Summer Time"),
    "Europe/Athens": ("Eastern European Standard Time", "Eastern European Summer Time"),
    "Europe/Istanbul": ("GMT+03:00", "GMT+03:00"),
    "Europe/Kyiv": ("Eastern European Standard Time", "Eastern European Summer Time"),
    "Europe/Lisbon": ("Western European Standard Time", "Western European Summer Time"),
    # 北美
    "America/Los_Angeles": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Denver": ("Mountain Standard Time", "Mountain Daylight Time"),
    "America/Phoenix": ("Mountain Standard Time", "Mountain Standard Time"),
    "America/Chicago": ("Central Standard Time", "Central Daylight Time"),
    "America/New_York": ("Eastern Standard Time", "Eastern Daylight Time"),
    "America/Anchorage": ("Alaska Standard Time", "Alaska Daylight Time"),
    "Pacific/Honolulu": ("Hawaii-Aleutian Standard Time", "Hawaii-Aleutian Standard Time"),
    "America/Toronto": ("Eastern Standard Time", "Eastern Daylight Time"),
    "America/Vancouver": ("Pacific Standard Time", "Pacific Daylight Time"),
    "America/Edmonton": ("Mountain Standard Time", "Mountain Daylight Time"),
    "America/Winnipeg": ("Central Standard Time", "Central Daylight Time"),
    "America/Halifax": ("Atlantic Standard Time", "Atlantic Daylight Time"),
    "America/St_Johns": ("Newfoundland Standard Time", "Newfoundland Daylight Time"),
    "America/Mexico_City": ("Central Standard Time", "Central Daylight Time"),
    "America/Sao_Paulo": ("Brasilia Standard Time", "Brasilia Summer Time"),
    # 大洋洲
    "Australia/Sydney": ("Australian Eastern Standard Time", "Australian Eastern Daylight Time"),
    "Australia/Melbourne": ("Australian Eastern Standard Time", "Australian Eastern Daylight Time"),
    "Australia/Hobart": ("Australian Eastern Standard Time", "Australian Eastern Daylight Time"),
    "Australia/Brisbane": ("Australian Eastern Standard Time", "Australian Eastern Standard Time"),
    "Australia/Adelaide": ("Australian Central Standard Time", "Australian Central Daylight Time"),
    "Australia/Darwin": ("Australian Central Standard Time", "Australian Central Standard Time"),
    "Australia/Perth": ("Australian Western Standard Time", "Australian Western Standard Time"),
    "Australia/Lord_Howe": ("Lord Howe Standard Time", "Lord Howe Daylight Time"),
    "Australia/Broken_Hill": ("Australian Central Standard Time", "Australian Central Daylight Time"),
    "Pacific/Auckland": ("New Zealand Standard Time", "New Zealand Daylight Time"),
    "Pacific/Chatham": ("Chatham Standard Time", "Chatham Daylight Time"),
}


def is_known_country(country: str) -> bool:
    """国家必须同时具有语言和本国有效时区映射，不能仅有白名单条目。"""
    default = COUNTRY_DEFAULT_TIMEZONE.get(country)
    return bool(_COUNTRY_LANGUAGE_TAG.get(country) and default
                and canonical_iana(default) in COUNTRY_TIMEZONES.get(country, ()))


def canonical_iana(tz_name: str) -> str:
    """返回别名对应的规范时区名；无别名映射时原样返回。"""
    return _IANA_ALIASES.get(tz_name, tz_name)


def timezone_offset_for(tz_name: str, dt: datetime | None = None) -> int:
    """按实际日期计算该 IANA 时区相对 UTC 的分钟偏移（DST 感知）。"""
    try:
        zone = ZoneInfo(tz_name)
        ref = dt or datetime.now(zone)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=zone)
        else:
            ref = ref.astimezone(zone)
        offset = ref.utcoffset()
        if offset is not None:
            return int(offset.total_seconds() // 60)
    except Exception:
        raise RegistrationGeoError("地域时区数据不可用", CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE) from None
    raise RegistrationGeoError("地域时区偏移不可确认", CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE)


def timezone_display_name(tz_name: str, dt: datetime | None = None) -> str:
    """返回与真实运行时一致的 DST 感知时区长名，不回退宿主时区或静态夏令时名称。"""
    try:
        zone = ZoneInfo(tz_name)
        ref = dt or datetime.now(zone)
        if ref.tzinfo is None:
            ref = ref.replace(tzinfo=zone)
        else:
            ref = ref.astimezone(zone)
        pair = _TZ_DST_LONG_NAMES.get(canonical_iana(tz_name))
        if pair:
            return pair[1] if bool(ref.dst()) else pair[0]
        return ref.tzname() or tz_name
    except Exception:
        raise RegistrationGeoError("地域时区数据不可用", CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE) from None


def resolve_timezone(country: str, observed_tz: str) -> str:
    """返回与 country 相容的 IANA 时区。

    显式画像时区须与画像国家相容；缺省使用该国家映射，不读取外部 GeoIP 时区。
    """
    observed = str(observed_tz or "").strip()
    if observed:
        try:
            ZoneInfo(observed)
        except Exception:
            raise RegistrationGeoError(
                f"地域信息不完整：国家 {country} 无有效时区观测",
                CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE,
            )
        canonical = canonical_iana(observed)
        allowed = COUNTRY_TIMEZONES.get(country, frozenset())
        if canonical in allowed or observed in allowed:
            return observed
        raise RegistrationGeoError(
            f"地域不一致：国家 {country} 与时区 {observed} 不相容",
            CATEGORY_GEO_MISMATCH, STAGE_VALIDATE,
        )
    default = COUNTRY_DEFAULT_TIMEZONE.get(country)
    if default:
        return default
    raise RegistrationGeoError(
        f"地域信息不完整：国家 {country} 无时区观测且无默认映射",
        CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE,
    )


def build_registration_locale(country: str, timezone: str = "") -> dict:
    """基于画像国家构建冻结语言/时区画像，绝不使用默认日本或 en-US/UTC 兜底。

    缺省 timezone 按国家选合法确定时区；显式 timezone 校验自身合法且与国家相容。
    """
    if not is_known_country(country):
        raise RegistrationGeoError("地域国家不受支持", CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE)
    timezone = resolve_timezone(country, timezone)
    tag = _COUNTRY_LANGUAGE_TAG.get(country)
    if not tag:
        raise RegistrationGeoError(
            f"地域信息不完整：未知国家 {country}",
            CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE,
        )
    base_language = tag.split("-", 1)[0]
    languages = [tag]
    if base_language != tag:
        languages.append(base_language)
    if base_language != "en":
        languages.extend(["en-US", "en"])
        accept_language = f"{tag},{base_language};q=0.9,en-US;q=0.8,en;q=0.7"
    else:
        languages.append("en")
        accept_language = f"{tag},en;q=0.9"
    navigator_language = tag
    navigator_languages = list(dict.fromkeys(
        part.split(";", 1)[0].strip() for part in accept_language.split(",") if part.strip()
    ))
    return {
        "navigator_language": navigator_language,
        "navigator_languages": navigator_languages,
        "accept_language": accept_language,
        "timezone_iana": timezone,
        "timezone_offset_minutes": timezone_offset_for(timezone),
        "timezone_name": timezone_display_name(timezone),
    }


# ----------------------------------------------------------------------
# 会话侧门禁：在校验通过后把画像冻结到 session 上。
# 这些函数接收 BrowserSession，但模块本身不依赖 transport，方向为 register → transport。
# ----------------------------------------------------------------------


def classify_geo_exception(exc: Exception) -> tuple[str, str]:
    """仅在确有代理证据时判为代理失败，否则归为地域服务不可达。"""
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    proxy_markers = (
        "407", "proxy authentication", "proxy-authorization", "proxy auth",
        "proxy requires auth", "connection to proxy", "proxy connect", "socks",
    )
    if "proxyerror" in name or any(marker in msg for marker in proxy_markers):
        return CATEGORY_PROXY, "代理连接/认证失败"
    return CATEGORY_GEO_SERVICE, f"网络异常({type(exc).__name__})"


def validate_session_geo(session, trace: dict | None = None) -> None:
    """用会话当前 trace 确认实际国家；不读取 GeoIP 服务或历史缓存。

    首次通过时冻结画像，后续只复核出口是否变化并保留冻结画像。
    """
    if not session.server_device_id:
        return
    trace = trace or {}
    country = session.normalize_country_code(trace.get("loc"))
    try:
        ip = str(ip_address(str(trace.get("ip") or "").strip()))
    except ValueError:
        raise RegistrationGeoError("地域信息不完整：缺少有效出口 IP",
                                   CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE) from None
    if not is_known_country(country):
        raise RegistrationGeoError("地域信息不完整：出口国家缺失或不受支持",
                                   CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE)
    session.exit_geo_observation_scope = "latest_trace"
    observed = {"ip": ip, "country": country, "sources": ["trace"]}
    if session.geo_frozen:
        _revalidate_session_geo(session, observed)
        return
    profile_country, supplied, source = _profile_selection(session, country)
    locale = build_registration_locale(profile_country, supplied.get("timezone_iana", ""))
    _validate_explicit_profile(supplied, locale)
    frozen = {**observed, **locale, "profile_country": profile_country, "locale_source": source}
    _freeze_session_geo(session, frozen)
    session._enforce_proxy_quality()


def _profile_selection(session, actual_country: str) -> tuple[str, dict, str]:
    from config import browser as browser_cfg

    supplied = session._explicit_browser_profile
    geo = supplied.get("geo") or {}
    has_country = isinstance(geo, dict) and any(field in geo for field in ("country_code", "country"))
    country_value = (geo.get("country_code") or geo.get("country")) if has_country else None
    key = supplied.get("locale_profile")
    has_locale = any(field in supplied for field in (
        "navigator_language", "navigator_languages", "accept_language", "timezone_iana"))
    if not has_country and not key and not has_locale and not browser_cfg.AUTO_BROWSER_LOCALE_FROM_IP:
        key = browser_cfg.BROWSER_LOCALE_PROFILE
        supplied = dict(browser_cfg.BROWSER_LOCALE_PROFILES.get(key) or {})
    if has_country:
        country = session.normalize_country_code(country_value)
    elif key:
        # 完整 profile key（jp/us 等）与 geo:xx 都可明确国家；不要反查复用模板的 AU/CA。
        country = session.normalize_country_code(str(key).removeprefix("geo:"))
    elif has_locale or not browser_cfg.AUTO_BROWSER_LOCALE_FROM_IP:
        country = ""
    else:
        return actual_country, {}, "auto"
    if not is_known_country(country):
        raise RegistrationGeoError("手动画像国家缺失或不受支持",
                                   CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE)
    return country, supplied, "manual"


def _validate_explicit_profile(supplied: dict, locale: dict) -> None:
    fields = ("navigator_language", "navigator_languages", "accept_language", "timezone_iana")
    if not any(field in supplied for field in fields):
        return
    language = str(supplied.get("navigator_language") or "").strip()
    languages = supplied.get("navigator_languages")
    accept = str(supplied.get("accept_language") or "").strip()
    accepted = [part.split(";", 1)[0].strip().lower() for part in accept.split(",") if part.strip()]
    if (not language or not isinstance(languages, (list, tuple)) or not languages
            or not accepted or not supplied.get("timezone_iana")):
        raise RegistrationGeoError("显式画像语言或时区信息不完整",
                                   CATEGORY_GEO_INCOMPLETE, STAGE_VALIDATE)
    normalized = [str(value).strip().lower() for value in languages]
    expected = locale["navigator_language"].lower()
    if (language.lower() != expected or normalized[0] != language.lower()
            or accepted[0] != language.lower() or any(value not in accepted for value in normalized)):
        raise RegistrationGeoError("显式画像语言与画像国家或 HTTP 偏好不一致",
                                   CATEGORY_GEO_MISMATCH, STAGE_VALIDATE)
    locale.update(navigator_language=language, navigator_languages=list(languages), accept_language=accept)


def _freeze_session_geo(session, frozen: dict) -> None:
    session.geo_frozen = True
    session.geo_frozen_profile = dict(frozen)
    session.exit_geo = {key: frozen[key] for key in ("ip", "country", "sources")}
    profile = session.browser_profile
    profile["geo"] = dict(session.exit_geo)
    for field in ("navigator_language", "accept_language", "timezone_iana",
                  "timezone_offset_minutes", "timezone_name", "profile_country", "locale_source"):
        profile[field] = frozen[field]
    profile["navigator_languages"] = list(frozen["navigator_languages"])
    logger.info("[注册地域] 画像冻结 actual_country=%s profile_country=%s source=%s tz=%s",
                frozen["country"], frozen["profile_country"], frozen["locale_source"], frozen["timezone_iana"])


def _revalidate_session_geo(session, observed: dict) -> None:
    changed = (observed["ip"] != session.exit_geo.get("ip")
               or observed["country"] != session.exit_geo.get("country"))
    session.exit_geo = observed
    session.browser_profile["geo"] = dict(observed)
    logger.info("[注册地域] 后续观测 actual_country=%s profile_country=%s changed=%s，保留冻结画像",
                observed["country"], session.browser_profile["profile_country"], changed)


__all__ = [
    "CATEGORY_GEO_INCOMPLETE", "CATEGORY_GEO_MISMATCH", "CATEGORY_GEO_PARSE",
    "CATEGORY_GEO_SERVICE", "CATEGORY_PROXY", "COUNTRY_DEFAULT_TIMEZONE", "COUNTRY_TIMEZONES",
    "RegistrationGeoError", "STAGE_DETECT", "STAGE_FREEZE", "STAGE_REVALIDATE", "STAGE_VALIDATE",
    "build_registration_locale", "canonical_iana", "classify_geo_exception", "is_known_country",
    "resolve_timezone", "timezone_display_name", "timezone_offset_for", "validate_session_geo",
]
