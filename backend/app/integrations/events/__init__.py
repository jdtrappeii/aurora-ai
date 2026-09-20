"""Free external-event stack.

  ticketmaster.py  concerts, pro sports, family shows near each store  (Discovery API, free key)
  seatgeek.py      the same, second opinion; de-duplicated against Ticketmaster
  fl511.py         FDOT road closures, crashes, roadwork near each store (FL511 developer key)
  calendar.py      federal + state holidays and the cannabis retail calendar (offline)
  sync.py          runs whichever providers have keys and upserts into external_events

Every provider returns EventDraft rows (common.py); nothing here touches the
database except sync.upsert_events. Providers take an httpx.Client so the tests
run against httpx.MockTransport and never the network.
"""
