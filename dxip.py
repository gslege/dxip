import sys
import re
from typing import List, Set, Tuple, Optional

import requests
from bs4 import BeautifulSoup


TARGET_URL = "https://api.uouin.com/cloudflare.html"


def _parse_speed_to_mbps(speed_text: str) -> Optional[Tuple[float, str]]:
    """
    Extract a numeric download speed from text and convert it to Mbps for sorting.
    Returns (mbps_value, original_display) or None if not found.
    """
    # Examples: 12.3 MB/s, 850 KB/s, 1.2 GB/s, 500 Mbps, 900 Kbps, 1.5 Gbps
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB/s|MB/s|KB/s|Gbps|Mbps|Kbps)", speed_text, re.IGNORECASE)
    if not m:
        return None
    value = float(m.group(1))
    unit = m.group(2).lower()

    if unit == "gb/s":
        mbps = value * 8 * 1000
    elif unit == "mb/s":
        mbps = value * 8
    elif unit == "kb/s":
        mbps = value * 8 / 1000
    elif unit == "gbps":
        mbps = value * 1000
    elif unit == "mbps":
        mbps = value
    elif unit == "kbps":
        mbps = value / 1000
    else:
        return None

    # Keep original matched display (normalized spacing and unit case from input)
    display = f"{m.group(1)} {m.group(2)}"
    return mbps, display


def extract_telecom_ips(html: str) -> List[Tuple[str, float, str]]:
    """
    Parse the HTML to extract unique IPv4 addresses from rows where the provider is 电信,
    alongside a detected download speed. Returns list of (ip, mbps, display_speed).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Find all tables and scan rows; be resilient to structure changes.
    tables = soup.find_all("table")
    seen_ips: Set[str] = set()
    results: List[Tuple[str, float, str]] = []
    ip_regex = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

    for table in tables:
        # Try to detect header indices first
        header_cells = None
        thead = table.find("thead")
        if thead:
            header_row = thead.find("tr")
            if header_row:
                header_cells = header_row.find_all(["th", "td"]) or None
        if header_cells is None:
            # Fallback: use the first row as header if it contains th
            first_tr = table.find("tr")
            if first_tr and first_tr.find("th"):
                header_cells = first_tr.find_all(["th", "td"]) or None

        ip_col = provider_col = speed_col = None
        if header_cells:
            headers_text = [c.get_text(strip=True) for c in header_cells]
            for idx, text in enumerate(headers_text):
                lower = text.lower()
                # IP column detection
                if ip_col is None and ("ip" in lower or "地址" in text):
                    ip_col = idx
                # Provider/line column detection
                if provider_col is None and ("线路" in text or "运营商" in text or "isp" in lower):
                    provider_col = idx
                # Speed column detection
                if speed_col is None and ("速度" in text or "speed" in lower):
                    speed_col = idx

        # Decide body rows
        body_rows = []
        tbody = table.find("tbody")
        if tbody:
            body_rows = tbody.find_all("tr")
        else:
            # All trs except the header row if we used it
            all_trs = table.find_all("tr")
            if header_cells and all_trs:
                body_rows = all_trs[1:]
            else:
                body_rows = all_trs

        # If we have header indices, use column-specific extraction
        if ip_col is not None and provider_col is not None and speed_col is not None:
            for tr in body_rows:
                tds = tr.find_all(["td", "th"])  # some tables repeat th in body
                if not tds or len(tds) <= max(ip_col, provider_col, speed_col):
                    continue
                provider_text = tds[provider_col].get_text(strip=True)
                if "电信" not in provider_text:
                    continue

                ip_text = tds[ip_col].get_text(" ", strip=True)
                match = ip_regex.search(ip_text)
                if not match:
                    continue
                ip = match.group(0)
                octets = ip.split(".")
                if not all(0 <= int(o) <= 255 for o in octets):
                    continue

                speed_text = tds[speed_col].get_text(" ", strip=True)
                parsed = _parse_speed_to_mbps(speed_text)
                if parsed:
                    mbps_value, display_speed = parsed
                else:
                    mbps_value, display_speed = 0.0, speed_text or "未知"

                if ip not in seen_ips:
                    seen_ips.add(ip)
                    results.append((ip, mbps_value, display_speed))
            continue  # proceed to next table

        # Fallback: row-wide text parsing
        for tr in body_rows:
            cells = tr.find_all(["td", "th"])  # include th in case headers carry labels
            if not cells:
                continue
            texts = [c.get_text(strip=True) for c in cells]
            row_text = " ".join(texts)

            if "电信" not in row_text:
                continue

            speed_parsed = _parse_speed_to_mbps(row_text)
            mbps_value: float = 0.0
            display_speed: str = "未知"
            if speed_parsed:
                mbps_value, display_speed = speed_parsed

            for match in ip_regex.findall(row_text):
                octets = match.split(".")
                if all(0 <= int(o) <= 255 for o in octets):
                    if match not in seen_ips:
                        seen_ips.add(match)
                        results.append((match, mbps_value, display_speed))
    # Sort by mbps descending
    results.sort(key=lambda x: x[1], reverse=True)
    return results


def fetch_html(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    # Avoid inheriting system proxy settings that could break SSL
    session = requests.Session()
    session.trust_env = False
    try:
        resp = session.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text
    except Exception:
        # Fallback: try again with SSL verification disabled (for restrictive proxies)
        resp = session.get(url, headers=headers, timeout=20, verify=False)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or resp.encoding
        return resp.text


def main() -> int:
    try:
        html = fetch_html(TARGET_URL)
    except Exception as e:
        print(f"请求网页失败: {e}")
        return 1

    items = extract_telecom_ips(html)
    if not items:
        print("未找到电信线路的IP地址。可能页面结构已变或需要浏览器渲染。")
        return 2

    # Write to dx.txt and also print to console, format: "IP  Cloudflare-<速度>"
    lines = [
        f"{ip}  Cloudflare-{display}"  # two spaces between IP and Cloudflare
        for ip, mbps, display in items
    ]
    try:
        with open("dx.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"写入dx.txt失败: {e}")
        return 3

    for line in lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())


