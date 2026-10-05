from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .http import StudyClient


DISCOVERY_URL = "https://www.canvard.net.cn"


def discover_domains(client: StudyClient | None = None) -> list[str]:
    if client is None:
        with StudyClient(DISCOVERY_URL) as session:
            return discover_domains(session)
    response = client.get_page()
    if response is None:
        return []
    soup = BeautifulSoup(response.text, "html.parser")
    domains = []
    for link in soup.select("a[href]"):
        title = link.find("h3")
        if title is None or "课程" not in title.get_text(strip=True):
            continue
        href = link.get("href")
        if not isinstance(href, str):
            continue
        domain = urlparse(urljoin(DISCOVERY_URL, href)).netloc
        if domain:
            domains.append(domain)
    return list(dict.fromkeys(domains))
