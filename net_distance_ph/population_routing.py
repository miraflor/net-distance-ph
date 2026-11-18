"""Network-distance engine for optional barangay-population representative sites.

Route *only* on a separate core that preserves every barangay demand terminal.
The existing 1,642-town core does NOT preserve barangay terminals and cannot be
used as a substitute. Inter-site distances are streamed into per-site row
checkpoints; no 42,010 x 42,010 distance matrix is ever built.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
from pathlib import Path
import time

import numpy as np
from numba import njit
from scipy.sparse.csgraph import connected_components, dijkstra

from . import graphbuild as gb
from . import matrix as mx
from . import reduce as rd
from .population_weighted import (SCHEMA, digest, write_json, read_points, validate_join,
    select_sites, select_sites_balanced, combine_group_matrices, finish_means, save_csv_matrix, filter_origin_rows)
from .population_data import load_plain_dataset, export_bundled_defaults


# Population-weighted logging instrumentation (v44)
from contextvars import ContextVar as _PwContextVar
from functools import wraps as _pw_wraps
from .workspace import timed_step as _pw_timed_step
# Unified routing progress instrumentation (v47)
from .workspace import progress_event as _pw_progress

_PW_HEARTBEAT_SECONDS = _PwContextVar("pw_heartbeat_seconds", default=30.0)


def _pw_time_call(label, function, *args, **kwargs):
    """Time a single operation without modifying its arguments or return value."""
    with _pw_timed_step(label, _PW_HEARTBEAT_SECONDS.get()):
        return function(*args, **kwargs)


def _pw_timed_phase(label):
    """Time an entire computation, including failures and cached returns."""
    def decorate(function):
        @_pw_wraps(function)
        def timed(*args, **kwargs):
            step = label(*args, **kwargs) if callable(label) else label
            with _pw_timed_step(step, _PW_HEARTBEAT_SECONDS.get()):
                return function(*args, **kwargs)
        return timed
    return decorate


@njit(cache=True)
def _shortest_to_targets(ptr, nei, weights, source, targets,
                         dist, dist_epoch, target_epoch, target_slot,
                         heap_node, heap_dist, epoch):
    """Heap Dijkstra, terminating after all within-component targets settle.

    Arrays are allocated once and epochs avoid full-graph clearing between
    barangay searches. Every edge weight must be finite and nonnegative.
    """
    for j in range(targets.size):
        target_epoch[targets[j]] = epoch
        target_slot[targets[j]] = j
    result = np.full(targets.size, np.inf, dtype=np.float64)
    remaining = targets.size
    dist[source] = 0.0
    dist_epoch[source] = epoch
    heap_node[0] = source
    heap_dist[0] = 0.0
    count = 1
    while count > 0 and remaining > 0:
        node = heap_node[0]
        weight = heap_dist[0]
        count -= 1
        if count > 0:
            last_n = heap_node[count]
            last_d = heap_dist[count]
            j = 0
            while True:
                child = 2 * j + 1
                if child >= count:
                    break
                if child + 1 < count and heap_dist[child + 1] < heap_dist[child]:
                    child += 1
                if last_d <= heap_dist[child]:
                    break
                heap_node[j] = heap_node[child]
                heap_dist[j] = heap_dist[child]
                j = child
            heap_node[j] = last_n
            heap_dist[j] = last_d
        if dist_epoch[node] != epoch or weight != dist[node]:
            continue
        if target_epoch[node] == epoch:
            result[target_slot[node]] = weight
            remaining -= 1
        for loc in range(ptr[node], ptr[node + 1]):
            dest = nei[loc]
            alt = weight + weights[loc]
            if dist_epoch[dest] != epoch or alt < dist[dest]:
                dist[dest] = alt
                dist_epoch[dest] = epoch
                if count >= heap_node.size:
                    raise ValueError("Dijkstra heap capacity exceeded")
                i = count
                count += 1
                while i > 0:
                    par = (i - 1) // 2
                    if heap_dist[par] <= alt:
                        break
                    heap_node[i] = heap_node[par]
                    heap_dist[i] = heap_dist[par]
                    i = par
                heap_node[i] = dest
                heap_dist[i] = alt
    return result


class TargetDijkstra:
    def __init__(self, G):
        n=G.shape[0]
        self.ptr=np.ascontiguousarray(G.indptr)
        self.nei=np.ascontiguousarray(G.indices)
        self.w=np.ascontiguousarray(G.data)
        self.dist=np.empty(n,dtype=np.float64)
        self.seen=np.zeros(n,dtype=np.int32)
        self.tseen=np.zeros(n,dtype=np.int32)
        self.tslot=np.zeros(n,dtype=np.int32)
        # Every successful relaxation visits a directed edge at most once.
        self.heap_node=np.empty(self.nei.size+1,dtype=np.int32)
        self.heap_dist=np.empty(self.nei.size+1,dtype=np.float64)
        self.epoch=0

    def get(self, src,targets):
        self.epoch+=1
        if self.epoch==2**31-1:
            self.seen.fill(0);self.tseen.fill(0);self.epoch=1
        return _shortest_to_targets(self.ptr,self.nei,self.w,int(src),
            np.asarray(targets,dtype=np.int32),self.dist,self.seen,
            self.tseen,self.tslot,self.heap_node,self.heap_dist,self.epoch)


@_pw_timed_phase("barangay terminal core (build/reuse)")
def points_core(workspace, home, prepared, pop_meta, points_path, params):
    """Create/reuse a *different* core protecting all positive-population points."""
    national_graph=workspace / "work" / "graph_full.npz"
    original_sources=workspace / "work" / "sources.npz"
    if not national_graph.is_file() or not original_sources.is_file():
        raise FileNotFoundError("National town graph not built: work/graph_full.npz and work/sources.npz required")
    filehash=digest(national_graph)
    # Match stage-1 provenance of the already built national graph; don't
    # silently mix an unrelated graph or move to a different transport release.
    with np.load(original_sources,allow_pickle=False) as orig:
        source_fingerprint=json.loads(str(orig["input_manifest"]))["fingerprint"]
    # Shapefile identity spans DBF (PSGC codes), SHX, PRJ, and geometry SHP.
    # Hash every existing sidecar; a changed DBF must invalidate the cache.
    sidecars = ([points_path.with_suffix(ext) for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg")]
                if points_path.suffix.lower() == ".shp" else [points_path])
    point_hashes={p.suffix.lower():digest(p) for p in sidecars if p.is_file()}
    keydata={"schema":SCHEMA,"national_graph_sha256":filehash,
             "graph_source_fingerprint":source_fingerprint,
             "population_sha256":pop_meta["data_file_sha256"],
             "units_sha256":pop_meta["source"]["units_sha256"],
             "points_file_sha256":point_hashes,"max_snap_km":params["max_snap_km"],
             "unsnappable_policy":params["unsnappable_policy"]}
    key=hashlib.sha256(json.dumps(keydata,sort_keys=True).encode()).hexdigest()
    core_dir=home / "cores" / key[:16]
    meta=core_dir / "metadata.json"
    required=["core_data.npy","core_indices.npy","core_indptr.npy","core_shape.npy",
              "core_index_per_bar.npy","original_index_per_bar.npy","offset_per_bar.npy",
              "snapped_node_lonlat.npy","valid_barangay_mask.npy","snap_audit.csv"]
    if meta.is_file():
        old=json.loads(meta.read_text(encoding="utf-8"))
        if old.get("cache_key")==key and all((core_dir/f).is_file() for f in required):
            print("REUSE: exact barangay-terminal-preserving core")
            return core_dir,key
    if core_dir.exists():
        raise RuntimeError(f"A different or incomplete barangay core exists at {core_dir}. Refusing to overwrite. Move it aside manually to rebuild; existing town core remains intact.")
    core_dir.mkdir(parents=True,exist_ok=False)
    positions=np.asarray(params["positions"],dtype=np.float64)
    try:
        with np.load(national_graph,allow_pickle=False) as g:
            ll=g["node_lonlat"]
            n_nodes=len(ll)
            # Match the national project road/ferry preference. Exclude
            # unreasonably distant locations rather than invent connections.
            idx,off,tier=gb.snap_sources(positions,ll,g["flags"],
                    max_snap_km=params["max_snap_km"],prefer_road=True,
                    fallback_terminals_only=False,verbose=True)
            bad=np.flatnonzero(tier<0)
            good=tier>=0
            if not np.any(good):
                raise ValueError("All positive-population origins failed snapping; no matrix can be constructed")
            if bad.size and params["unsnappable_policy"]=="error":
                examples=[prepared[i]["psgc_code"] for i in bad[:10]]
                raise ValueError(f"{bad.size} barangays unsnappable within {params['max_snap_km']}km; examples {examples}. No synthetic links created.")
            if bad.size:
                missing_pop=sum(int(prepared[i]["population"]) for i in bad)
                print(f"SNAP COVERAGE WARNING: {len(bad)} origins / {missing_pop:,} residents "
                      "cannot be attached within the snap limit; excluded from routing, "
                      "retained in population-coverage denominators",flush=True)
            snapped_ll=np.full((len(idx),2),np.nan,dtype=np.float64)
            snapped_ll[good]=ll[idx[good]]
            del ll
            print("De-duplicating original graph edges...")
            u,v,w=rd.dedupe_edges(g["u"],g["v"],g["w"],n_nodes)
        from .fast_reduce import reduce_fast
        print(f"Exact fast reduction protecting {np.unique(idx[good]).size:,} valid barangay terminals...")
        u,v,w,alive=reduce_fast(u,v,w,n_nodes,np.unique(idx[good]),progress=print)
        cu,cv,cw,cn,valid_core_sources=rd.compact(u,v,w,alive,idx[good])
        del u,v,w,alive
        G=mx.build_csr(cu,cv,cw,cn)
        mx.save_core(G,core_dir)
        csrc=np.full(idx.shape,-1,dtype=np.int32)
        csrc[good]=valid_core_sources.astype(np.int32)
        np.save(core_dir/"core_index_per_bar.npy",csrc)
        np.save(core_dir/"original_index_per_bar.npy",idx.astype(np.int32))
        np.save(core_dir/"offset_per_bar.npy",off)
        np.save(core_dir/"snapped_node_lonlat.npy",snapped_ll)
        np.save(core_dir/"valid_barangay_mask.npy",good.astype(np.bool_))
        audit_rows=[{"psgc_code":prepared[i]["psgc_code"],
                     "barangay_name":prepared[i].get("barangay_name",""),
                     "town_psgc":prepared[i]["town_psgc"],
                     "population":int(prepared[i]["population"]),
                     "lon":f"{positions[i,0]:.9f}","lat":f"{positions[i,1]:.9f}",
                     "reason":f"no modeled road/ferry node within {params['max_snap_km']} km"}
                    for i in bad]
        from .population_data import _write_csv
        _write_csv(core_dir/"snap_audit.csv",
                   ("psgc_code","barangay_name","town_psgc","population","lon","lat","reason"),audit_rows)
        write_json(meta,{**keydata,"cache_key":key,"n_core_vertices":cn,"n_core_edges":len(cu),
              "n_barangays":len(prepared),"n_unmodeled_barangays":len(bad),
              "unmodeled_population":sum(int(prepared[i]["population"]) for i in bad),
              "n_unique_terminals":len(np.unique(valid_core_sources))})
        print(f"BARANGAY CORE READY: {cn:,} nodes / {len(cu):,} edges")
        return core_dir,key
    except BaseException:
        # Preserve failed artifacts for forensic inspection, but never publish
        # metadata that would make a partial core resumable.
        meta.unlink(missing_ok=True)
        raise


@_pw_timed_phase("adaptive k-medians selection")
def build_town_sites(home, core_dir, key, rows, roster, params):
    """Town-sized exact local network D matrices; checkpoint selected sites per town."""
    selection_started = time.monotonic()  # v48 progress clock
    G=mx.load_core(core_dir,mmap=True)
    ncomp,label=connected_components(G,directed=False)
    # Population is precisely the published 2024 barangay weight.
    core_idx=np.load(core_dir/"core_index_per_bar.npy")
    original_idx=np.load(core_dir/"original_index_per_bar.npy")
    off=np.load(core_dir/"offset_per_bar.npy")
    coords=np.load(core_dir/"snapped_node_lonlat.npy")
    bytown={}
    for i,r in enumerate(rows):
        if r["population"]>0 and core_idx[i]>=0:
            bytown.setdefault(r["town_psgc"],[]).append(i)
    codes=sorted(c for c,r in roster.items() if r.get("geographic_level") in {"City","Mun"})
    settings_hash=hashlib.sha256(json.dumps(params["selection"],sort_keys=True).encode()).hexdigest()[:16]
    cache_dir=home/"town_sites"/key/settings_hash
    cache_dir.mkdir(parents=True,exist_ok=True)
    solver=TargetDijkstra(G)
    all_sites=[]
    audit=[]
    selection_mode=params["selection"].get("mode", "one")
    for ni,town in enumerate(codes,1):
        ids=bytown.get(town,[])
        if not ids:
            # All local origins were outside the snap radius. Preserve this
            # town in output with zero modeled reachability and full census
            # population. Never synthesize a road connection.
            print(f"SNAP COVERAGE WARNING: no modeled origins in town {town}",flush=True)
            if "median_selection" in params["selection"]:
                audit.append({"town_psgc":town,"town_name":roster[town]["name"],
                              "mode":selection_mode,"n_demand_nodes":0,
                              "n_components":0,"n_sites":0,"mean_access_km":"",
                              "selection_score":"","component_site_counts":"{}"})
            continue
        cache=cache_dir/(town+".json")
        if cache.is_file():
            obj=json.loads(cache.read_text(encoding="utf-8"))
            if obj.get("schema")!=SCHEMA or obj.get("settings")!=params["selection"]:
                raise ValueError(f"Town site cache has mismatched settings: {cache}")
            all_sites.extend(obj["sites"])
            if "median_selection" in params["selection"]:
                diagnostic=obj.get("selection_diagnostics")
                if diagnostic is None:
                    raise ValueError(f"Median selection audit missing from checkpoint: {cache}")
                audit.append({"town_psgc":town,"town_name":roster[town]["name"],
                              "mode":selection_mode,"n_demand_nodes":obj["n_demand_nodes"],
                              "n_components":diagnostic["n_components"],"n_sites":len(obj["sites"]),
                              "mean_access_km":obj["mean_access_km"],
                              "selection_score":diagnostic.get("selection_score", ""),
                              "component_site_counts":json.dumps(diagnostic["component_site_counts_by_graph_component"],sort_keys=True)})
            continue
        # Multiple barangay representatives may snap to one road vertex.
        # Aggregate their populations, *without* discarding their snap offsets.
        merged={}
        for i in ids:
            node=int(core_idx[i]);orig=int(original_idx[i]);p=int(rows[i]["population"])
            if node not in merged:
                merged[node]={"core_node":node,"original_node":orig,"population":0,
                              "stub_sum":0.,"lon":float(coords[i,0]),"lat":float(coords[i,1])}
            merged[node]["population"]+=p
            merged[node]["stub_sum"]+=p*float(off[i])
        demand=[merged[x] for x in sorted(merged)]
        nodes=np.array([d["core_node"] for d in demand],dtype=np.int32)
        pops=np.array([d["population"] for d in demand],dtype=float)
        stubs=np.array([d["stub_sum"]/d["population"] for d in demand])
        comps=label[nodes]
        distances=np.full((len(nodes),len(nodes)),np.inf)
        # Never run a full-component search to look for unreachable targets.
        for comp in np.unique(comps):
            ix=np.flatnonzero(comps==comp)
            targets=nodes[ix]
            for q,j in enumerate(ix):
                distances[j,ix]=solver.get(nodes[j],targets)
        # Core shortest-path distances must be symmetric to floating tolerance.
        finite=np.isfinite(distances)
        if not np.array_equal(finite,finite.T):raise ValueError(f"Town {town} reachability asymmetry")
        if finite.any() and np.max(np.abs(distances[finite]-distances.T[finite]))>1e-7:
            raise ValueError(f"Town {town} local distance asymmetry")
        if selection_mode=="balanced":
            chosen,weights,access,overall,diagnostic=select_sites_balanced(
                distances,pops,stubs,max_sites=params["selection"]["max_sites_per_town"])
        elif selection_mode in ("one","budget"):
            chosen,weights,access,overall=select_sites(distances,pops,stubs,
                max_mean_access_km=params["selection"]["max_mean_access_km"],
                max_sites=params["selection"]["max_sites_per_town"])
            diagnostic={"criterion":selection_mode,"n_components":len(np.unique(comps)),
                        "n_demand_nodes":len(nodes),"n_sites":len(chosen),
                        "component_site_counts":[int(np.isin(chosen,np.flatnonzero(comps==c)).sum())
                                                 for c in np.unique(comps)]}
        else:
            raise ValueError(f"Unknown median selection mode {selection_mode!r}")
        diagnostic["component_site_counts_by_graph_component"]={
            str(int(c)):int(np.count_nonzero(comps[chosen]==c)) for c in np.unique(comps)}
        sites=[]
        for j, ix in enumerate(chosen):
            d=demand[ix]
            sites.append({"town_psgc":town,"core_node":d["core_node"],
                          "original_node":d["original_node"],
                          "lon":d["lon"],"lat":d["lat"],
                          "population":int(weights[j]),"mean_access_km":float(access[j]),
                          "component":int(comps[ix])})
        assert sum(x["population"] for x in sites)==int(pops.sum())
        write_json(cache,{"schema":SCHEMA,"settings":params["selection"],
                          "population":int(pops.sum()),"mean_access_km":overall,
                          "n_demand_nodes":len(nodes),"selection_diagnostics":diagnostic,
                          "sites":sites})
        all_sites.extend(sites)
        if "median_selection" in params["selection"]:
            audit.append({"town_psgc":town,"town_name":roster[town]["name"],
                          "mode":selection_mode,"n_demand_nodes":len(nodes),
                          "n_components":diagnostic["n_components"],"n_sites":len(sites),
                          "mean_access_km":overall,
                          "selection_score":diagnostic.get("selection_score", ""),
                          "component_site_counts":json.dumps(diagnostic["component_site_counts_by_graph_component"],sort_keys=True)})
        if ni%100==0 or ni==len(codes):
            _pw_progress("adaptive k-medians selection", ni, len(codes), selection_started, unit="towns")
    if "median_selection" in params["selection"]:
        from .population_data import _write_csv
        _write_csv(cache_dir/"selection_audit.csv",
                   ("town_psgc","town_name","mode","n_demand_nodes","n_components",
                    "n_sites","mean_access_km","selection_score","component_site_counts"),audit)
        print(f"MEDIAN SELECTION: {selection_mode}; {len(all_sites):,} sites; "
              f"audit {cache_dir/'selection_audit.csv'}",flush=True)
    return all_sites


_ROUTE_G=None
_ROUTE_NODES=None
_ROUTE_POP=None
_ROUTE_TOWN=None
_ROUTE_NUM_TOWN=None

def _routing_init(core_dir, nodes, population, town_indices, n_towns):
    global _ROUTE_G,_ROUTE_NODES,_ROUTE_POP,_ROUTE_TOWN,_ROUTE_NUM_TOWN
    _ROUTE_G=mx.load_core(core_dir,mmap=True)
    _ROUTE_NODES=np.asarray(nodes,dtype=np.int32)
    _ROUTE_POP=np.asarray(population,dtype=np.float64)
    _ROUTE_TOWN=np.asarray(town_indices,dtype=np.int32)
    _ROUTE_NUM_TOWN=int(n_towns)


def _routing_task(item):
    i,src=item
    d=dijkstra(_ROUTE_G,directed=False,indices=int(src))[_ROUTE_NODES]
    ok=np.isfinite(d)
    numer=np.bincount(_ROUTE_TOWN[ok], weights=_ROUTE_POP[ok]*d[ok],minlength=_ROUTE_NUM_TOWN)
    reach=np.bincount(_ROUTE_TOWN[ok], weights=_ROUTE_POP[ok],minlength=_ROUTE_NUM_TOWN)
    return i,numer,reach


@_pw_timed_phase("OD routing")
def compute_routing(home, core_dir, sites, key, *, workers=2, all_towns=None):
    """Per-unique-source memoization, streaming to memmaps; crash-recoverable."""
    import multiprocessing as mp
    towns=sorted(all_towns) if all_towns is not None else sorted(set(s["town_psgc"] for s in sites))
    if not towns:raise ValueError("No populated towns available")
    ti={v:i for i,v in enumerate(towns)}
    site_nodes=np.array([s["core_node"] for s in sites],dtype=np.int32)
    site_pop=np.array([s["population"] for s in sites],dtype=np.float64)
    site_town=np.array([ti[s["town_psgc"]] for s in sites],dtype=np.int32)
    unique_nodes=np.unique(site_nodes)
    route_hash=hashlib.sha256((key+json.dumps(sites,sort_keys=True)).encode()).hexdigest()
    route_dir=home/"routing"/route_hash
    route_dir.mkdir(parents=True,exist_ok=True)
    header=route_dir/"meta.json"
    schema={"schema":SCHEMA,"key":route_hash,"n_unique_sites":len(unique_nodes),
            "n_sites":len(sites),"n_towns":len(towns)}
    numpath=route_dir/"numerator_by_site_town.npy"
    denpath=route_dir/"reachable_pop_by_site_town.npy"
    donepath=route_dir/"finished_sources.npy"
    if header.exists():
        old=json.loads(header.read_text(encoding="utf-8"))
        if old!=schema:raise ValueError("Routing checkpoint metadata mismatch")
        if not (numpath.exists() and denpath.exists() and donepath.exists()):
            raise ValueError("Incomplete routing checkpoint; refusing unsafe resume")
        numer=np.load(numpath,mmap_mode="r+")
        reach=np.load(denpath,mmap_mode="r+")
        done=np.load(donepath)
        if numer.shape!=(len(unique_nodes),len(towns)) or reach.shape!=numer.shape or done.shape!=(len(unique_nodes),):
            raise ValueError("Routing checkpoint array shapes differ")
    else:
        from numpy.lib.format import open_memmap
        numer=open_memmap(numpath,mode="w+",dtype=np.float64,shape=(len(unique_nodes),len(towns)))
        reach=open_memmap(denpath,mode="w+",dtype=np.float64,shape=(len(unique_nodes),len(towns)))
        numer[:]=np.nan;reach[:]=np.nan; numer.flush();reach.flush()
        done=np.zeros(len(unique_nodes),dtype=np.bool_)
        np.save(donepath,done)
        write_json(header,schema)
    todo=[(i,int(n)) for i,n in enumerate(unique_nodes) if not done[i]]
    print(f"OD routing: {len(unique_nodes):,} unique site nodes, {len(todo):,} pending; {max(1,workers)} worker(s)",flush=True)
    def persist():
        numer.flush();reach.flush()
        temp=donepath.with_name("finished_sources.tmp.npy")
        np.save(temp,done)
        os.replace(temp,donepath)
    routing_progress_started = time.monotonic()
    if todo:
        workers=max(1,int(workers))
        proc=None
        try:
            if workers>1:
                ctx=mp.get_context("spawn")
                proc=ctx.Pool(workers,initializer=_routing_init,
                     initargs=(str(core_dir),site_nodes,site_pop,site_town,len(towns)))
                gen=proc.imap_unordered(_routing_task,todo,chunksize=1)
            else:
                _routing_init(str(core_dir),site_nodes,site_pop,site_town,len(towns))
                gen=map(_routing_task,todo)
            last=0
            for count,(i,nu,de) in enumerate(gen,1):
                numer[i]=nu;reach[i]=de;done[i]=True
                if count%16==0 or count==len(todo):
                    persist()
                if count%max(1,len(todo)//10)==0 or count==len(todo):
                    _pw_progress("OD routing", count, len(todo), routing_progress_started, unit="new source nodes")
            if proc is not None:
                proc.close();proc.join();proc=None
        except BaseException:
            if proc is not None:proc.terminate();proc.join()
            # Only the durably flushed completion mask is authority; on a
            # crash/restart, unfinished rows are recomputed and overwritten.
            raise
    if not np.all(done):raise RuntimeError("OD routing incomplete")
    if not np.isfinite(numer).all() or not np.isfinite(reach).all():
        raise ValueError("Completed routing contains nonfinite row totals")
    # Route once per unique site node; many sites may share a road node.
    node_to_index=np.searchsorted(unique_nodes,site_nodes)
    A=np.zeros((len(towns),len(towns)),dtype=np.float64)
    B=np.zeros_like(A)
    for s,idx in enumerate(node_to_index):
        A[site_town[s]]+=site_pop[s]*numer[idx]
        B[site_town[s]]+=site_pop[s]*reach[idx]
    return towns,A,B,route_hash


@_pw_timed_phase(lambda *a, **kw: f"matrix export: {a[1] if len(a) > 1 else kw.get('level', 'unknown')}")
def _save_level(home,level,units,chosen_mapping, town_num,town_den,town_pop,sites,
                extra, *, route_hash, core_key, full_town_pop=None, snap_audit=None, town_codes=None):
    from .population_weighted import finish_means, combine_group_matrices, save_csv_matrix
    codes=[u["code"] for u in units]
    site_towns=sorted({s["town_psgc"] for s in sites}) if town_codes is None else list(town_codes)
    ti={t:i for i,t in enumerate(site_towns)}
    access_total=np.zeros(len(site_towns),dtype=float)
    for s in sites:
        access_total[ti[s["town_psgc"]]]+=s["population"]*s["mean_access_km"]
    if full_town_pop is None:
        full_town_pop=town_pop
    full_town_pop=np.asarray(full_town_pop,dtype=float)
    if level=="town":
        numerator,denominator=town_num,town_den
        pop=full_town_pop
        modeled_pop=town_pop
        access_sum=access_total
    else:
        numerator,denominator=combine_group_matrices(town_num,town_den,chosen_mapping,len(units))
        mapping=np.asarray(chosen_mapping)
        keep=mapping>=0
        pop=np.bincount(mapping[keep],weights=full_town_pop[keep],minlength=len(units)).astype(np.float64)
        modeled_pop=np.bincount(mapping[keep],weights=town_pop[keep],minlength=len(units)).astype(np.float64)
        access_sum=np.bincount(mapping[keep],weights=access_total[keep],minlength=len(units)).astype(np.float64)
    mean_access=np.divide(access_sum,modeled_pop,out=np.zeros_like(modeled_pop),where=modeled_pop>0)
    cond,coverage=finish_means(numerator,denominator,pop)
    bound=np.divide(mean_access[:,None]+mean_access[None,:],coverage,
           out=np.full_like(coverage,np.inf),where=coverage>0)
    # Results are immutable per routing fingerprint, so changing the access
    # budget never silently replaces an earlier scientific run.
    target=home/"output"/route_hash[:16]/level
    target.mkdir(parents=True,exist_ok=True)
    previous=target/"manifest.json"
    if previous.is_file():
        old=json.loads(previous.read_text(encoding="utf-8"))
        if old.get("routing_hash")!=route_hash or old.get("core_cache_key")!=core_key:
            raise ValueError("Output fingerprint collision/mismatch; refusing to overwrite")
    np.save(target/"distance_matrix_km.npy",cond)
    np.save(target/"reachable_pair_probability.npy",coverage)
    np.save(target/"population_per_unit.npy",pop)
    np.save(target/"modeled_population_per_unit.npy",modeled_pop)
    np.save(target/"origin_population_coverage.npy",np.divide(modeled_pop,pop,
           out=np.zeros_like(pop),where=pop>0))
    np.save(target/"mean_representation_access_km.npy",mean_access)
    np.save(target/"distance_approximation_bound_km.npy",bound)
    save_csv_matrix(target/"distance_matrix_km.csv",cond,units)
    with (target/"units.csv").open("w",newline="",encoding="utf-8") as fd:
        wr=csv.writer(fd);wr.writerow(["row","psgc_code","name","population"])
        for i,u in enumerate(units):wr.writerow([i,u["code"],u["name"],f"{pop[i]:.0f}"])
    stats={"schema":SCHEMA,"level":level,"distance_definition":"conditional population-weighted expectation of shortest distances between representative road nodes; NOT centroid distance and NOT expected individual trip length",
           "diagonal":"Within-unit expected distance between two independent population draws; not forced to zero",
           "reachability":"reachable_pair_probability is a conservative fraction of total census population-pairs with demonstrated modeled routes; unsnappable origin population is included in the denominator and counted as UNMODELED (not proven disconnected); zero probability -> +inf",
           "n_units":len(units),"n_sites":len(sites),"n_reachable_undirected_offdiagonal_unit_pairs":int(np.isfinite(cond[np.triu_indices(len(units),1)]).sum()),
           "zero_population_units":[u["code"] for u,p in zip(units,pop) if p==0],
           "excluded_population_by_level":float(extra["population_total"]-pop.sum()),
           "total_population_in_level":float(pop.sum()),
           "modeled_population_in_level":float(modeled_pop.sum()),
           "unmodeled_origin_population":float(pop.sum()-modeled_pop.sum()),
           "snap_audit_csv":str(snap_audit) if snap_audit else None,
           "missing_geometry_audit_csv":extra.get("missing_geometry_audit_csv"),
           "missing_geometry_barangays":extra.get("missing_geometry_barangays",0),
           "missing_geometry_population":extra.get("missing_geometry_population",0),
           "representative_approximation_bound":"(mean_access_i+mean_access_j)/reachable_pair_probability; conservative km bound relative to the model's barangay point-and-stub network distance, not real-world travel distance",
           "province_crosswalk_sha256":extra.get("province_crosswalk_sha256"),
           "core_cache_key":core_key,"routing_hash":route_hash,
           "population_source":extra["source"]}
    write_json(target/"manifest.json",stats)
    print(f"WROTE {level} {len(units)} x {len(units)} population-weighted distance and reachability matrices: {target}")
    return target


@_pw_timed_phase("independent network verification")
def verify_original_graph(workspace, core_dir, sites, n_checked):
    """Independent global shortest-path fidelity check against original graph."""
    if n_checked<=0:return {"checked":0,"warning":"Independent original-graph verification disabled by user"}
    unique_site={s["original_node"]:s["core_node"] for s in sites}
    original_nodes=np.array(sorted(unique_site),dtype=np.int64)
    core_nodes=np.array([unique_site[int(x)] for x in original_nodes],dtype=np.int32)
    n_checked=min(n_checked,len(original_nodes))
    picks=np.random.default_rng(24062026).choice(len(original_nodes),n_checked,replace=False)
    with np.load(workspace/"work"/"graph_full.npz",allow_pickle=False) as g:
        u,v,w=g["u"],g["v"],g["w"]
        n=g["node_lonlat"].shape[0]
        u,v,w=rd.dedupe_edges(u,v,w,n)
        original=mx.build_csr(u,v,w,n)
    core=mx.load_core(core_dir,mmap=True)
    n_mismatched=0;max_error=0.0
    for i in picks:
        ref=dijkstra(original,directed=False,indices=int(original_nodes[i]))[original_nodes]
        got=dijkstra(core,directed=False,indices=int(core_nodes[i]))[core_nodes]
        n_mismatched+=int(np.count_nonzero(np.isfinite(ref)!=np.isfinite(got)))
        both=np.isfinite(ref)&np.isfinite(got)
        if both.any():max_error=max(max_error,float(np.max(np.abs(ref[both]-got[both]))))
    result={"checked":n_checked,"n_reference_site_pairs":len(original_nodes)*n_checked,
            "reachability_mismatches":n_mismatched,"max_absolute_error_km":max_error,
            "tolerance_km":1e-6,"passed":n_mismatched==0 and max_error<=1e-6}
    if not result["passed"]:raise ValueError(f"Independent original-graph verification failed: {result}")
    print(f"VERIFIED: {n_checked} original graph sources against {len(original_nodes)} representative sites; max error {max_error:.3e}km; 0 reachability mismatches")
    return result


def _validated_province_crosswalk(source, roster):
    if source is None:
        return {}, None
    from .population_weighted import code10
    path=Path(source).expanduser().resolve()
    known_towns={c for c,r in roster.items() if r.get("geographic_level") in {"City","Mun"}}
    known_provinces={c for c,r in roster.items() if r.get("geographic_level")=="Prov"}
    crosswalk={}
    with path.open(newline="",encoding="utf-8-sig") as fd:
        reader=csv.DictReader(fd)
        if not {"town_psgc","province_psgc"}.issubset(set(reader.fieldnames or [])):
            raise ValueError("Province crosswalk requires town_psgc,province_psgc columns")
        for row in reader:
            town,prov=code10(row["town_psgc"]),code10(row["province_psgc"])
            if town not in known_towns or prov not in known_provinces or town in crosswalk:
                raise ValueError(f"Invalid/duplicated geographic province mapping {(town,prov)}")
            old=roster[town]["province_psgc"]
            if old and old!=prov:
                raise ValueError(f"Province crosswalk would override existing PSGC province {old} for {town}")
            crosswalk[town]=prov
    if not crosswalk:
        raise ValueError("An empty province crosswalk provides no mappings")
    return crosswalk,digest(path)


def run(args):
    """One-command optional run; tables and geometry are prepared as needed."""
    workspace=Path(args.workspace).expanduser().resolve()
    home=workspace/"variants"/"population_weighted"
    repo=Path(__file__).resolve().parents[1]
    default_dir=repo/"reference"/"population"/"default"
    dataset_dir=Path(args.dataset_dir).expanduser().resolve() if args.dataset_dir else default_dir
    if args.dataset_dir is None:
        if not (dataset_dir/"units.csv").is_file() or not (dataset_dir/"barangay_population.csv").is_file():
            print("Preparing normalized default CSV tables once...",flush=True)
            export_bundled_defaults(repo,dataset_dir)
    roster,rows,pop_meta=_pw_time_call("load population dataset", load_plain_dataset, dataset_dir,
        allow_missing_population=getattr(args,"allow_missing_population",False))
    print(f"DATASET: {len(roster):,} administrative units; {len(rows):,} positive-population barangays; "
          f"population {pop_meta['population_total']:,}",flush=True)
    if args.max_sites_per_town<1:
        raise ValueError("--max-sites-per-town must be positive")
    if args.max_mean_access_km is not None and (not np.isfinite(args.max_mean_access_km) or args.max_mean_access_km<0):
        raise ValueError("--max-mean-access-km must be nonnegative and finite")
    if args.verify_sites<0:raise ValueError("--verify-sites must be nonnegative")
    if args.workers<1:raise ValueError("--workers must be >=1")
    crosswalk,crosswalk_sha=_validated_province_crosswalk(args.province_crosswalk,roster)
    if crosswalk_sha:
        pop_meta={**pop_meta,"province_crosswalk_sha256":crosswalk_sha}
        print(f"Validated geographic province crosswalk for {len(crosswalk)} towns",flush=True)
    from .population_geometry import discover_points
    points_path=_pw_time_call("discover barangay geometry", discover_points, workspace,dataset_dir,roster,
        requested=args.points,auto_geometry=not getattr(args,"no_auto_geometry",False),
        source=getattr(args,"geometry_source","ocha"))
    points=_pw_time_call("read barangay geometry", read_points, points_path)
    full_population_rows=rows
    rows,coordinates,missing_geometry=_pw_time_call("validate geometry coverage", filter_origin_rows,
        full_population_rows,points,
        on_missing=getattr(args,"missing_geometry_policy","exclude"))
    missing_pop=sum(int(r["population"]) for r in missing_geometry)
    from .population_data import _write_csv
    # This separate audit is written before routing, so even an interrupted
    # national computation leaves a durable record of incomplete coverage.
    geometry_audit=home/"geometry"/(
        f"missing_origins_{digest(points_path)[:12]}_{pop_meta['data_file_sha256'][:12]}.csv")
    _write_csv(geometry_audit,
        ("psgc_code","barangay_name","town_psgc","population","reason"),
        [{"psgc_code":r["psgc_code"],"barangay_name":r["barangay_name"],
          "town_psgc":r["town_psgc"],"population":int(r["population"]),
          "reason":"no matched barangay geometry under selected source policy"}
         for r in missing_geometry])
    if missing_geometry:
        print(f"GEOMETRY COVERAGE WARNING: skipping {len(missing_geometry):,} "
              f"positive-population barangays ({missing_pop:,} residents); "
              f"retained in coverage denominator. Audit: {geometry_audit}",flush=True)
    pop_meta={**pop_meta,"missing_geometry_audit_csv":str(geometry_audit),
              "missing_geometry_barangays":len(missing_geometry),
              "missing_geometry_population":missing_pop}
    if args.max_mean_access_km is not None and getattr(args,"median_selection","balanced")=="one":
        raise ValueError("--median-selection one conflicts with --max-mean-access-km; "
                         "omit one to use budgeted k-median selection")
    selection_mode=("budget" if args.max_mean_access_km is not None
                    else getattr(args,"median_selection","one"))
    selection={"max_mean_access_km":args.max_mean_access_km,
               "max_sites_per_town":args.max_sites_per_town,
               "median_selection":getattr(args,"median_selection","one"),
               "mode":selection_mode}
    if args.max_snap_km<=0 or not np.isfinite(args.max_snap_km):
        raise ValueError("--max-snap-km must be finite positive")
    params={"max_snap_km":args.max_snap_km,"positions":coordinates,"selection":selection,
            "unsnappable_policy":getattr(args,"unsnappable_policy","exclude")}
    core_dir,key=points_core(workspace,home,rows,pop_meta,points_path,params)
    sites=build_town_sites(home,core_dir,key,rows,roster,params)
    if len(sites)>16000:
        raise ValueError(f"{len(sites)} representatives would be expensive; reduce --max-sites-per-town, raise access tolerance, or explicitly review capacity")
    all_towns=sorted(c for c,r in roster.items() if r["level"] in ("city","municipality"))
    towns,town_num,town_den,route_hash=compute_routing(home,core_dir,sites,key,
         workers=getattr(args,"workers",2),all_towns=all_towns)
    output_run=home/"output"/route_hash[:16]
    output_run.mkdir(parents=True,exist_ok=True)
    selection_cache_hash=hashlib.sha256(json.dumps(selection,sort_keys=True).encode()).hexdigest()[:16]
    selection_audit=home/"town_sites"/key/selection_cache_hash/"selection_audit.csv"
    write_json(home/"town_sites"/key/selection_cache_hash/"selection_policy.json",
        {"median_selection":selection_mode,"max_sites_per_town":args.max_sites_per_town,
         "max_mean_access_km":args.max_mean_access_km,
         "automatic_objective":(
             "weighted within-component road access divided by one-median baseline "
             "+ fraction of optional demand-node representatives retained; "
             "approximately optimized by greedy additions and 1-swaps"
             if selection_mode=="balanced" else None),
         "selection_audit_csv":str(selection_audit)})
    sites_path=output_run/"representative_sites.csv"
    with sites_path.open("w",newline="",encoding="utf-8") as fd:
        fields=["town_psgc","core_node","original_node","lon","lat","population","mean_access_km","component"]
        wr=csv.DictWriter(fd,fieldnames=fields);wr.writeheader();wr.writerows(sites)
    popsite_by_town={t:0 for t in towns}
    for s in sites:popsite_by_town[s["town_psgc"]]+=s["population"]
    town_pop=np.array([popsite_by_town[t] for t in towns],dtype=float)
    original_population_by_town={t:0 for t in towns}
    for record in full_population_rows:original_population_by_town[record["town_psgc"]]+=record["population"]
    full_town_pop=np.array([original_population_by_town[t] for t in towns],dtype=float)
    snap_audit=core_dir/"snap_audit.csv"
    print(f"POPULATION COVERAGE: {int(town_pop.sum()):,}/{int(full_town_pop.sum()):,} "
          "residents represented by snapped barangay origins",flush=True)
    town_units=[{"code":t,"name":roster[t]["name"]} for t in towns]
    province_codes=sorted(c for c,r in roster.items() if r.get("level")=="province")
    region_codes=sorted(c for c,r in roster.items() if r.get("level")=="region")
    pm={c:i for i,c in enumerate(province_codes)}
    rm={c:i for i,c in enumerate(region_codes)}
    prov_map=np.array([pm.get(roster[t]["province_psgc"],-1) for t in towns],dtype=int)
    for i,t in enumerate(towns):
        if t in crosswalk:prov_map[i]=pm[crosswalk[t]]
    reg_map=np.array([rm.get(roster[t]["region_psgc"],-1) for t in towns],dtype=int)
    if (reg_map<0).any():
        raise ValueError(f"Town-to-region mapping incomplete: {[towns[i] for i in np.flatnonzero(reg_map<0)][:10]}")
    check=verify_original_graph(workspace,core_dir,sites,args.verify_sites)
    write_json(output_run/"independent_original_graph_verification.json",check)
    if args.level in {"all","town"}:
        _save_level(home,"town",town_units,np.arange(len(towns)),town_num,town_den,town_pop,sites,pop_meta,route_hash=route_hash,core_key=key,
                    full_town_pop=full_town_pop,snap_audit=snap_audit,town_codes=towns)
    if args.level in {"all","province"}:
        print(f"PROVINCE ACCOUNTING: {int(np.count_nonzero(prov_map<0))} towns excluded; population {int(town_pop[prov_map<0].sum()):,}")
        _save_level(home,"province",[{"code":c,"name":roster[c]["name"]} for c in province_codes],prov_map,town_num,town_den,town_pop,sites,pop_meta,route_hash=route_hash,core_key=key,
                    full_town_pop=full_town_pop,snap_audit=snap_audit,town_codes=towns)
    if args.level in {"all","region"}:
        _save_level(home,"region",[{"code":c,"name":roster[c]["name"]} for c in region_codes],reg_map,town_num,town_den,town_pop,sites,pop_meta,route_hash=route_hash,core_key=key,
                    full_town_pop=full_town_pop,snap_audit=snap_audit,town_codes=towns)
    write_json(home/"output"/"latest_run.json",{"schema":SCHEMA,"routing_hash":route_hash,
        "directory":str(output_run),"levels": ["town","province","region"] if args.level=="all" else [args.level],
        "median_selection":selection_mode,"selection_audit_csv":str(selection_audit),
        "verification_file":str(output_run/"independent_original_graph_verification.json")})
    print("COMPLETE: baseline centroid matrix, selected graph releases and original raw datasets unchanged")
    return 0
