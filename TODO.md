# TODO

## Benchmark against existing tools

Add a `bench/` script (needs `hyperfine`, `fd`, `rg`, `parallel-ssh` or GNU `parallel`) that prints one table.
Run every case with a cold cache (`--prepare 'sync; echo 3 | sudo tee /proc/sys/vm/drop_caches'`) and a warm one,
and check that every tool returns the same number of results (same skips: hidden files, `/proc`, sshfs mounts).

- [ ] **search, one host**: `tailmon search` vs `find ... -prune` vs `fd --no-ignore --exclude /proc`
- [ ] **grep, one host**, on a fixed tree: `tailmon grep` vs `grep -rI` vs `rg --no-ignore`
- [ ] **search, all hosts**: `tailmon search` vs `parallel-ssh -h hosts.txt -i "find ..."` vs
      `parallel --nonall -S ...`; same SSH `ControlMaster` setting for all; also compare bytes over the network
- [ ] **monitoring**: cost of the probe on each host (`/usr/bin/time -v`: CPU, peak RAM), time to sample all hosts
      (`hyperfine './tailmon.py --json'`), and against `node_exporter`, `glances` and Netdata at the same interval

Baseline so far: guarded `search /` on the local machine, 1.1 M files in 42 s (cache state unknown).
