"""
Uniform pagination + search/filter helper, used by every list page
(Trunks, DIDs, Routing Rules, Rate Table entries, Subscribers, Nodes).
Keeps the pattern genuinely identical everywhere rather than a
slightly-different implementation per page.
"""


def get_page_size():
    import db
    rows = db.query("SELECT default_page_size FROM platform_settings WHERE id=1")
    return rows[0]["default_page_size"] if rows else 25


def paginate_query(base_sql, count_sql, params, request_args, search_column=None, order_by=None,
                    page_param="page", q_param="q"):
    """
    base_sql: SELECT ... FROM ... WHERE 1=1  (filters get appended --
              do NOT include ORDER BY here, pass it via order_by
              instead, or filters would get appended after it,
              producing invalid SQL)
    count_sql: SELECT COUNT(*) FROM ... WHERE 1=1  (same filters)
    params: list of params already bound in base_sql/count_sql
    request_args: flask request.args
    search_column: column name for the free-text search box
    order_by: e.g. "region, name" -- applied after filters, before LIMIT/OFFSET
    page_param/q_param: override the query-string param names -- needed
              whenever a single page has more than one independently
              paginated table (e.g. a domain's Users table and its
              Enabled-on-SIP-Profiles table), so each table's paging
              doesn't stomp on the other's via a shared "page"/"q".
    Returns (rows, page, total_pages, total_count)
    """
    import db

    page_size = get_page_size()
    page = max(1, int(request_args.get(page_param, 1)))
    q = request_args.get(q_param, "").strip()

    where_extra = ""
    extra_params = []
    if q and search_column:
        where_extra += f" AND {search_column} ILIKE %s"
        extra_params.append(f"%{q}%")

    count_row = db.query(count_sql + where_extra, params + extra_params)
    total = count_row[0]["count"] if count_row else 0
    total_pages = max(1, -(-total // page_size))
    page = min(page, total_pages)
    offset = (page - 1) * page_size

    order_clause = f" ORDER BY {order_by}" if order_by else ""
    rows = db.query(
        base_sql + where_extra + order_clause + " LIMIT %s OFFSET %s",
        params + extra_params + [page_size, offset]
    )
    return rows, page, total_pages, total
