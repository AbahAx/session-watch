"""Map Xray online-map names to authoritative panel client identifiers."""

PREFIX = 'user>>>'
SUFFIX = '>>>online'
MAX_ONLINE_NAME = 256 + len(PREFIX) + len(SUFFIX)


def normalize_online_names(names, client_emails):
    known = set(client_emails)
    result = set()
    for name in names:
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_ONLINE_NAME:
            raise ValueError('Invalid online identifier')
        candidates = {name} if name in known else set()
        if name.startswith(PREFIX) and name.endswith(SUFFIX):
            email = name[len(PREFIX):-len(SUFFIX)]
            if email in known:
                candidates.add(email)
        # An unknown/ambiguous name is a failed observation, never a fabricated
        # client or a successful empty list that could trigger false departures.
        if len(candidates) != 1:
            raise ValueError('Online identifier does not uniquely match inventory')
        result.update(candidates)
    return result
