"""Edge cases of the address and name helpers."""

from __future__ import annotations

import hashlib

from collections import namedtuple

from custom_components.roomwise.util import (
    clean_charbuf,
    irk_tag,
    mac_norm,
    name_is_address,
    option_in_range,
    redact_addresses,
    snapshot_data,
)
from custom_components.roomwise.const import CONF_MAX_RADIUS


def test_mac_norm_accepts_dotted_and_bare_forms():
    assert mac_norm("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"
    assert mac_norm("AABBCCDDEEFF") == "aa:bb:cc:dd:ee:ff"
    assert mac_norm("AA-BB-CC-DD-EE-FF") == "aa:bb:cc:dd:ee:ff"
    assert mac_norm("AA_BB_CC_DD_EE_FF") == "aa:bb:cc:dd:ee:ff"


def test_mac_norm_leaves_unrecognised_text_lowercased():
    assert mac_norm("Not A Mac") == "not a mac"


def test_empty_or_missing_names_count_as_addresses():
    assert name_is_address(None)
    assert name_is_address("")
    assert name_is_address("roomwise_aa_bb_cc_00_00_01")
    assert not name_is_address("Living Room Proxy")


def test_irk_tag_is_stable_and_never_contains_the_key():
    key = "00112233445566778899aabbccddeeff"
    tag = irk_tag(key)
    assert tag == irk_tag(bytes.fromhex(key))
    assert key[:6] not in tag
    assert tag.startswith("irk#")


def test_irk_tag_copes_with_text_that_is_not_hex():
    tag = irk_tag("not-hex-at-all")
    expected = hashlib.sha256(b"not-hex-at-all").hexdigest()[:6]
    assert tag == f"irk#{expected}"


def test_option_in_range_rejects_things_that_are_not_numbers():
    assert option_in_range(CONF_MAX_RADIUS, 10.0)
    assert not option_in_range(CONF_MAX_RADIUS, "10")
    assert not option_in_range(CONF_MAX_RADIUS, None)
    assert not option_in_range(CONF_MAX_RADIUS, True)


def test_clean_charbuf_handles_missing_and_padded_text():
    assert clean_charbuf(None) == ""
    assert clean_charbuf("  Tag\x00junk  ") == "Tag"


class _Pair(namedtuple("_Pair", "a b")):
    pass


class _PlainTuple(tuple):
    pass


class _StrictTuple(tuple):
    def __new__(cls):
        return super().__new__(cls, (1, 2))


class _StrictSet(set):
    def __init__(self):
        super().__init__({1, 2})


def test_snapshot_copies_containers_but_shares_leaves():
    leaf = object()
    original = {"list": [leaf], "tuple": (leaf,), "set": {1, 2}, "frozen": frozenset({3})}
    copy = snapshot_data(original)
    assert copy == original
    assert copy["list"] is not original["list"]
    assert copy["list"][0] is leaf
    assert copy["set"] is not original["set"]
    assert type(copy["tuple"]) is tuple
    assert type(copy["frozen"]) is frozenset


def test_snapshot_keeps_the_kind_of_tuple_subclass_it_can_rebuild():
    assert snapshot_data(_Pair(1, [2])) == _Pair(1, [2])
    assert type(snapshot_data(_Pair(1, 2))) is _Pair
    assert type(snapshot_data(_PlainTuple((1, 2)))) is _PlainTuple


def test_snapshot_falls_back_to_a_plain_container_when_the_subclass_cannot_be_rebuilt():
    tupled = snapshot_data(_StrictTuple())
    assert type(tupled) is tuple
    assert tupled == (1, 2)
    setted = snapshot_data(_StrictSet())
    assert type(setted) is frozenset
    assert setted == frozenset({1, 2})


BEACON = "e2c56db5dffb48d2b060d0f5a71096e0"


def test_redaction_uses_the_table_entry_for_the_uuid_of_an_ibeacon_id():
    out = redact_addresses(f"seen {BEACON}_1_2 here", {BEACON: "beacon-1"})
    assert out == "seen beacon-1_1_2 here"


def test_redaction_masks_unknown_addresses_and_keys_generically():
    out = redact_addresses({"mac": "AA:BB:CC:DD:EE:FF", "uuid": f"{BEACON}_3_4"}, {})
    assert out == {"mac": "aa:xx:xx:xx:xx:ff", "uuid": "e2c5::REDACTED_3_4"}


def test_redaction_survives_an_object_whose_text_form_is_broken():
    class Broken:
        def __repr__(self):
            raise RuntimeError("no repr for you")

    broken = Broken()
    assert redact_addresses([broken], {})[0] is broken


def test_redaction_replaces_an_object_whose_text_form_holds_an_address():
    class Holder:
        def __repr__(self):
            return "Holder(AA:BB:CC:DD:EE:FF)"

    assert redact_addresses(Holder(), {}) == "Holder(aa:xx:xx:xx:xx:ff)"
