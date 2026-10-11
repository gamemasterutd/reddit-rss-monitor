def engagement_from_ranks(hot_rank=None, top_day_rank=None, top_week_rank=None, rising_rank=None):
    evidence = []
    active_feeds = 0
    if hot_rank is not None:
        evidence.append("Hot #%d" % hot_rank)
        active_feeds += 1
    if rising_rank is not None:
        evidence.append("Rising #%d" % rising_rank)
        active_feeds += 1
    if top_day_rank is not None:
        evidence.append("Top day #%d" % top_day_rank)
        active_feeds += 1
    if top_week_rank is not None:
        evidence.append("Top week #%d" % top_week_rank)
        active_feeds += 1

    # Appearing in more than one Reddit ranking surface is strong evidence even
    # if the individual positions are not top-10.
    if active_feeds >= 2:
        return "HIGH", "; ".join(evidence)
    if hot_rank is not None:
        return ("HIGH" if hot_rank <= 5 else "MEDIUM"), "; ".join(evidence)
    if rising_rank is not None:
        return ("HIGH" if rising_rank <= 10 else "MEDIUM"), "; ".join(evidence)
    if top_day_rank is not None:
        return ("HIGH" if top_day_rank <= 10 else "MEDIUM"), "; ".join(evidence)
    if top_week_rank is not None:
        return ("HIGH" if top_week_rank <= 10 else "MEDIUM"), "; ".join(evidence)
    return "LOW", "New only"
