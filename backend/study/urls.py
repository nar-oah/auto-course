from collections.abc import Iterable
from bs4 import BeautifulSoup, ResultSet, Tag
from requests import Response
from response import Call
from urllib.parse import urlparse


def get_urls() -> Iterable[str | None]:
    def get_links(html: str) -> ResultSet[Tag] | None:
        soup = BeautifulSoup(html, "html.parser")
        list_div = soup.select_one('div.list.set_index5[data-num="1"]')
        return list_div.select("a[href]") if isinstance(list_div, Tag) else None

    def get_url(link: Tag) -> str | None:
        url = link.get("href")
        return urlparse(url).netloc if isinstance(url, str) else None

    response = Call("https://www.canvard.net.cn").get_page()
    links = get_links(response.text) if isinstance(response, Response) else None
    if isinstance(links, ResultSet):
        links = links[:2]
        return map(lambda link: get_url(link), links)
    return []


if __name__ == "__main__":
    print(list(get_urls()))
