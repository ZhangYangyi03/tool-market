"""retrieval_probe.py -- does a filled dense leg actually beat BM25 here?

The repo's headline figures (bm25 .607/.893 vs hybrid .777/.955 on 112 labelled
queries) were measured on a shelf built elsewhere and are quoted, not
reproducible from this tree: the labelled set is not in it. Meanwhile the dense
leg had no caller at all (`Store.index_vector` was dead code) so every
deployment answered `mode: hybrid` while ranking exactly like BM25.

This probe answers the question that matters for a deployment: with the vectors
actually written, is the ranking better than lexical alone on THIS machine, and
by how much? It runs the same shelf and the same needs through both
configurations and prints both numbers, so the comparison is like-for-like.

Method, and its bias, stated up front:
  * the shelf is a fixed 40-tool set spanning file/network/data/text work;
  * the needs are written to avoid the tool's own name (`tcp_probe` is asked for
    as "check whether something is listening on a port"), because a query that
    reuses the identifier only measures tokenization;
  * that makes the query set paraphrase-heavy, which favours the dense leg --
    so the numbers below are an upper bound on the win for paraphrase-style
    asks and should not be quoted as "retrieval got X% better" in general;
  * Chinese needs are reported separately: BM25 over English documents cannot
    match them at all, so mixing them in would flatter the dense leg.

Run: python -m experiments.retrieval_probe   (needs an embedding key)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from toolmarket import embed, retrieval, vectorstore as vs  # noqa: E402

SHELF = [
    ("read_file", "Read the contents of a file on disk and return them as text."),
    ("write_file", "Write text to a path, creating parent directories when needed."),
    ("list_dir", "List the entries of a directory with their sizes and mtimes."),
    ("find_files", "Search under a directory for names matching a glob pattern."),
    ("http_get", "Fetch a URL over HTTP and return status, headers and body."),
    ("http_post", "Send a JSON body to a URL and return the parsed response."),
    ("dns_lookup", "Resolve a hostname to its IPv4 and IPv6 addresses."),
    ("ping_host", "Check whether a host answers ICMP echo within a timeout."),
    ("tcp_probe", "Return True if a TCP port on a host accepts a connection."),
    ("port_scan", "Report which of a small range of ports are open on a host."),
    ("ssh_exec", "Run one shell command on a remote machine over SSH."),
    ("scp_copy", "Copy one file between this machine and a remote host."),
    ("git_clone", "Clone a remote repository into a local directory."),
    ("git_push", "Push local commits to a remote repository on a branch."),
    ("git_diff", "Show the difference between the working tree and a commit."),
    ("tar_pack", "Pack a directory into a compressed tar archive."),
    ("unzip", "Extract a zip archive into a target directory."),
    ("hash_file", "Compute a SHA-256 digest of a file's bytes."),
    ("json_query", "Extract a nested value from JSON using a dotted path."),
    ("csv_to_table", "Parse comma separated text into rows of columns."),
    ("base64_codec", "Encode or decode binary data as base64 text."),
    ("regex_replace", "Rewrite text by substituting a pattern for a replacement."),
    ("text_diff", "Show which lines two blocks of text disagree on."),
    ("count_words", "Count words, lines and characters in a block of text."),
    ("slugify", "Turn a title into a lower case hyphenated identifier."),
    ("redact_secrets", "Mask API keys and passwords inside a log line."),
    ("parse_duration", "Convert a human duration like 1h30m into seconds."),
    ("seconds_to_hms", "Format an elapsed number of seconds as a clock string."),
    ("now_iso", "Return the current time in ISO 8601 with the local offset."),
    ("sleep_until", "Block until a given wall clock time has been reached."),
    ("run_python", "Execute a Python source string and capture its output."),
    ("run_shell", "Execute a shell command and capture its exit code and output."),
    ("cpu_probe", "Report the CPU model, core count and instruction set flags."),
    ("gpu_probe", "Report which CUDA devices are present with their memory."),
    ("mem_stats", "Report free and used memory on this machine."),
    ("disk_usage", "Report free space on each mounted filesystem."),
    ("process_list", "List running processes with pid, cpu and memory."),
    ("kill_pid", "Terminate a process by its pid, politely then forcibly."),
    ("sqlite_query", "Run a SQL statement against a sqlite file and return rows."),
    ("screenshot", "Capture the screen and save it as a PNG image."),
]

NEEDS_EN = [
    ("read_file", "show me what is inside a text file"),
    ("write_file", "save some text somewhere on disk"),
    ("list_dir", "what files are in this folder"),
    ("find_files", "locate every file whose name looks like this pattern"),
    ("http_get", "download a web page and give me the body"),
    ("http_post", "submit a payload to an api and read the answer"),
    ("dns_lookup", "turn a domain name into an address"),
    ("ping_host", "see if a machine is reachable on the network"),
    ("tcp_probe", "check whether something is listening on a port"),
    ("port_scan", "which services on this box are exposed"),
    ("ssh_exec", "run a command somewhere else over a network login"),
    ("scp_copy", "transfer a file to another computer"),
    ("git_clone", "get a copy of a repository from github"),
    ("git_push", "upload my commits to the server"),
    ("git_diff", "what did I change since the last commit"),
    ("tar_pack", "make an archive of this project directory"),
    ("unzip", "open up a compressed archive"),
    ("hash_file", "fingerprint a file so I can tell if it changed"),
    ("json_query", "pull one field out of a structured response"),
    ("csv_to_table", "read a spreadsheet export into rows"),
    ("base64_codec", "turn binary into printable characters and back"),
    ("regex_replace", "swap one pattern in a string for something else"),
    ("text_diff", "compare two versions of the same paragraph"),
    ("count_words", "how long is this piece of writing"),
    ("slugify", "make a url friendly version of this heading"),
    ("redact_secrets", "hide credentials that leak into logs"),
    ("parse_duration", "i typed 1h30m and need it as a number of seconds"),
    ("seconds_to_hms", "show a runtime as hours minutes seconds"),
    ("now_iso", "what time is it right now, written out"),
    ("sleep_until", "wait until a specific moment today"),
    ("run_python", "execute some python code on the host"),
    ("run_shell", "run a terminal command and give me the output"),
    ("cpu_probe", "what processor is in this machine"),
    ("gpu_probe", "do I have a graphics card available"),
    ("mem_stats", "how much ram is left"),
    ("disk_usage", "am I running out of storage space"),
    ("process_list", "what is currently running on this box"),
    ("kill_pid", "stop a program that is stuck"),
    ("sqlite_query", "query a local database file"),
    ("screenshot", "grab a picture of the screen"),
]

NEEDS_ZH = [
    ("read_file", "把磁盘上那个文本文件的内容读出来"),
    ("http_get", "抓一个网页然后把内容给我"),
    ("dns_lookup", "把域名解析成 ip 地址"),
    ("tcp_probe", "看看某个端口是不是有人监听"),
    ("seconds_to_hms", "把运行时长显示成时分秒"),
    ("git_push", "把本地提交推到服务器上"),
    ("cpu_probe", "这台机器用的是什么处理器"),
    ("disk_usage", "磁盘还剩多少空间"),
    ("redact_secrets", "把日志里的密钥遮掉"),
    ("screenshot", "截一张屏幕截图"),
]


def _records():
    return [{"id": "tool:" + n, "name": n, "description": d,
             "metadata": {"tags": []}, "contract": {"parameters": {"properties": {}}}}
            for n, d in SHELF]


def _metrics(fn, needs):
    hit1 = hit3 = hit5 = 0
    mrr = 0.0
    misses = []
    for want, q in needs:
        rows, _info = fn(q)
        ids = [r[0] for r in rows]
        rank = ids.index("tool:" + want) + 1 if "tool:" + want in ids else 0
        if rank == 1:
            hit1 += 1
        if rank and rank <= 3:
            hit3 += 1
        if rank and rank <= 5:
            hit5 += 1
        mrr += (1.0 / rank) if rank else 0.0
        if not rank or rank > 5:
            misses.append((q, want, ids[:3]))
    n = len(needs)
    return {"n": n, "hit@1": round(hit1 / n, 3), "hit@3": round(hit3 / n, 3),
            "hit@5": round(hit5 / n, 3), "mrr": round(mrr / n, 3), "misses": misses}


def main():
    tmp = tempfile.mkdtemp(prefix="probe_")
    records = _records()
    docs = dict((r["id"], vs.build_resource_text(r)) for r in records)

    st = vs.Store(os.path.join(tmp, "i.sqlite"))
    st.create_collection("tools", dim=embed.settings()["dim"],
                         model_id=embed.settings()["model"])
    st.sync_from_records("tools", records)

    def lex(q):
        return st.bm25_search("tools", q, k=5), None

    def hybrid(q):
        return retrieval.best_search(st, "tools", q, k=5, documents=docs,
                                     use_rerank=False)

    print("shelf:", len(records), "tools")
    print("embedder:", embed.settings()["model"], "dim", embed.settings()["dim"])
    before = _metrics(lex, NEEDS_EN)
    print("\n-- before backfill: bm25 only (vectors in collection: %d)"
          % st.stats("tools")["vectors"])
    print(json.dumps({k: v for k, v in before.items() if k != "misses"}, indent=None))
    for m in before["misses"]:
        print("   miss:", m[0], "->", m[1], "got", m[2])

    bf = retrieval.backfill_vectors(st, "tools", records=records)
    print("\nbackfill:", json.dumps(bf))

    after = _metrics(hybrid, NEEDS_EN)
    print("\n-- after backfill: hybrid (bm25 + dense)")
    print(json.dumps({k: v for k, v in after.items() if k != "misses"}, indent=None))
    for m in after["misses"]:
        print("   miss:", m[0], "->", m[1], "got", m[2])

    zh_before = _metrics(lex, NEEDS_ZH)
    zh_after = _metrics(hybrid, NEEDS_ZH)
    print("\n-- chinese needs, reported separately (n=%d)" % len(NEEDS_ZH))
    print("bm25  :", json.dumps({k: v for k, v in zh_before.items() if k != "misses"}))
    print("hybrid:", json.dumps({k: v for k, v in zh_after.items() if k != "misses"}))

    print("\nsummary")
    print("  english  hit@1 %.3f -> %.3f   hit@5 %.3f -> %.3f   mrr %.3f -> %.3f"
          % (before["hit@1"], after["hit@1"], before["hit@5"], after["hit@5"],
             before["mrr"], after["mrr"]))
    print("  chinese  hit@1 %.3f -> %.3f   hit@5 %.3f -> %.3f   mrr %.3f -> %.3f"
          % (zh_before["hit@1"], zh_after["hit@1"], zh_before["hit@5"],
             zh_after["hit@5"], zh_before["mrr"], zh_after["mrr"]))


if __name__ == "__main__":
    main()
