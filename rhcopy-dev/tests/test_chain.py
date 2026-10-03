"""Chain drops malformed RPC entries (e.g. a bare API key put into an env var) and keeps valid URLs,
so one typo in RPC_URL / ETH_RPC_URL / BSC_RPC_URL / BASE_RPC_URL no longer spams the log every scan."""
import logging

from rhcopy.chain import Chain


def test_chain_drops_non_url_rpc_entries():
    c = Chain(["alch_RhyGQ4qUhYEhfh_o4quh_", None, "https://eth.drpc.org", "ws://nope"], log=logging.getLogger("t"))
    assert c.urls == ["https://eth.drpc.org"]


def test_chain_keeps_valid_urls_in_order():
    c = Chain(["https://a.example", "http://b.example"])
    assert c.urls == ["https://a.example", "http://b.example"]
