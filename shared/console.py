def print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    """Print a plain-text table that remains readable in a terminal or saved log."""
    widths = [max(len(row[index]) for row in [headers, *rows]) for index in range(len(headers))]
    border = "+-" + "-+-".join("-" * width for width in widths) + "-+"

    def print_row(row: tuple[str, ...]) -> None:
        print("| " + " | ".join(value.ljust(width) for value, width in zip(row, widths)) + " |", flush=True)

    print(border, flush=True)
    print_row(headers)
    print(border, flush=True)
    for row in rows:
        print_row(row)
    print(border, flush=True)
