# Live searches and catalogue growth

A search displays saved results immediately, then the page POSTs independently to:

- `/search/source/{slug}/osm`: OpenStreetMap / Private.coffee Overpass.
- `/search/source/{slug}/wikidata`: Wikidata SPARQL (CC0).
- `/search/source/{slug}/gleif`: GLEIF legal-entity register (CC0).

Each request requires the session CSRF token, has an IP rate limit and a 50-second provider deadline. Successful source results are shared for five minutes; failures are not cached as empty successes. Source progress is shown separately. Matching names and website domains are merged with existing records and every fact retains its original source. Existing checked descriptions are preserved. Website enrichment runs afterward and reuses fresh provider candidates instead of immediately repeating their searches.

Google ratings/reviews are not enabled or scraped. The current deployment uses free open sources. Brave web search remains optional (`BRAVE_API_KEY`); it is not currently configured. This is federated business lookup, not an index of every website on the internet. Free sources can be incomplete or temporarily unavailable; a zero count is not proof that no businesses exist.

## Seed catalogue and growth

The initial registry import adds at least 10,000 active legal-entity records, rotating countries and preserving GLEIF source URLs. Registry entries are labelled separately from service-verified businesses. They do not imply confirmed services, trading activity, or endorsement. The small verified service list links directly to each business website.

The hourly Actions crawler bootstraps any deficit below 10,000, then aims for 1,000 new records per UTC day (subtracting additions already made that day). Fixed provider pagination and durable country/page cursors avoid repeatedly counting the first page as new. Source outages and small country registers can affect the rate. Inserts and updates are counted separately; a batch can exceed the target by fewer than 200 records. Crawler jobs continue collecting other business categories within the remaining execution budget.

Members may leave one review and one recommendation vote per business listing; edits update their own existing feedback. Community opinions are separate from collected facts and imported ratings. Forms require sign-in and CSRF tokens and escape review text. Public correction forms also accept abuse reports.
