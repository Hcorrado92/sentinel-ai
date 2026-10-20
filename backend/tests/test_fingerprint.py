from app.ingest.fingerprint import compute_fingerprint, normalize_path, parse_frames

TRACE_A = """Traceback (most recent call last):
  File "/srv/releases/1234/shop/api.py", line 42, in checkout
    total = compute_total(cart)
  File "/srv/releases/1234/shop/cart.py", line 17, in compute_total
    return sum(item.price for item in cart.items)
  File "/usr/lib/python3.11/site-packages/fastapi/routing.py", line 301, in run
    raise exc
TypeError: unsupported operand for <Item object at 0x7f3a2c1b9d30> (id=42)
"""

TRACE_B = """Traceback (most recent call last):
  File "/srv/releases/5678/shop/api.py", line 45, in checkout
    total = compute_total(cart)
  File "/srv/releases/5678/shop/cart.py", line 99, in compute_total
    return sum(item.price for item in cart.items)
  File "/home/ci/.venv/lib/python3.12/site-packages/fastapi/routing.py", line 310, in run
    raise exc
TypeError: unsupported operand for <Item object at 0x55d1e0a3f010> (id=7)
"""


def test_traces_differing_only_in_variable_values_share_fingerprint():
    assert compute_fingerprint("TypeError", TRACE_A) == compute_fingerprint("TypeError", TRACE_B)


def test_different_exception_type_changes_fingerprint():
    assert compute_fingerprint("TypeError", TRACE_A) != compute_fingerprint("KeyError", TRACE_A)


def test_different_function_name_changes_fingerprint():
    other = TRACE_A.replace("in compute_total", "in compute_subtotal")
    assert compute_fingerprint("TypeError", TRACE_A) != compute_fingerprint("TypeError", other)


def test_python_frames_are_normalized():
    assert parse_frames(TRACE_A, "TypeError") == [
        "shop/api.py:checkout",
        "shop/cart.py:compute_total",
        "fastapi/routing.py:run",
    ]


def test_windows_and_site_packages_paths_are_normalized():
    assert normalize_path(r"C:\deploy\app\shop\cart.py") == "shop/cart.py"
    assert normalize_path("/opt/venv/lib/python3.11/site-packages/pkg/mod.py") == "pkg/mod.py"
    assert normalize_path("shop/cart.py") == "shop/cart.py"


def test_generic_fallback_ignores_positions_addresses_and_uuids():
    first = (
        "Error: boom 3fa85f64-5717-4562-b3fc-2c963f66afa6\n"
        "    at handler (/srv/app/routes/index.js:10:5)\n"
        "    at Object.<anonymous> (0x7ffd1234) /srv/app/server.js:88:12\n"
    )
    second = (
        "Error: boom 6b29fc40-ca47-1067-b31d-00dd010662da\n"
        "    at handler (/home/dev/app/routes/index.js:14:9)\n"
        "    at Object.<anonymous> (0x1a2b3c) /home/dev/app/server.js:91:2\n"
    )
    assert compute_fingerprint("Error", first) == compute_fingerprint("Error", second)
    renamed = second.replace("at handler", "at otherHandler")
    assert compute_fingerprint("Error", first) != compute_fingerprint("Error", renamed)


def test_generic_fallback_without_frames_ignores_message_numbers():
    first = "RuntimeError: worker 12 crashed\nin job runner pid 3345"
    second = "RuntimeError: worker 99 crashed\nin job runner pid 18"
    assert compute_fingerprint("RuntimeError", first) == compute_fingerprint(
        "RuntimeError", second
    )


def test_fingerprint_is_sha256_hex():
    value = compute_fingerprint("ValueError", "")
    assert len(value) == 64
    assert int(value, 16) >= 0
