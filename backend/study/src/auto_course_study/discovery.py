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
    listing = soup.select_one('div.list.set_index5[data-num="1"]')
    if listing is None:
        return []
    domains = []
    for link in listing.select("a[href]")[:2]:
        href = link.get("href")
        if not isinstance(href, str):
            continue
        domain = urlparse(urljoin(DISCOVERY_URL, href)).netloc
        if domain:
            domains.append(domain)
    return domains
