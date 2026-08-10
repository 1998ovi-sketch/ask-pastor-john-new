from pathlib import Path
from email.utils import parsedate_to_datetime
from lxml import etree

p = Path("seed/ask-pastor-john.rss")
root = etree.parse(str(p)).getroot()
items = root.find("channel").findall("item")
dates = [parsedate_to_datetime(i.findtext("pubDate")) for i in items]
print("items:", len(items))
print("newest:", max(dates).isoformat())
print("oldest:", min(dates).isoformat())
print("oldest title:", min(items, key=lambda i: parsedate_to_datetime(i.findtext("pubDate"))).findtext("title"))
