You tag news items for a professional oil, gas, refining, petrochemical and
midstream newsletter that focuses on how geopolitics moves physical markets.

For each item return an object with:
  id         (copy it)
  segments   subset of {segments}
  nodes      ids from this exposure map that the item touches: {nodes}
  relevance  0-5 (5 = moves prices/flows/margins now; 0 = off-topic, PR fluff, duplicate)
  why        <= 20 words: the market mechanism, not a summary

Return ONLY a JSON array.

Items:
{items}
