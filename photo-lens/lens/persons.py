"""From face vectors to people: clustering, and keeping a person the same person.

Two problems, and only the first one is about faces.

**Clustering.** Nobody has told lens who is in these photographs, so the groups
have to be found. The vectors are unit-length (see lens/faces.py), so cosine
similarity is a dot product, and the rule is deliberately the simplest one that
works: walk the faces in a fixed order, put each one in the nearest existing
cluster if it is close enough, otherwise start a new cluster with it; then
recompute the centroids and do it again. Two passes, no library.

Hand-rolled rather than `sklearn.cluster.AgglomerativeClustering` on purpose.
Agglomerative clustering is O(n²) in memory (it wants the full distance matrix —
20,000 faces is a 1.6GB float32 triangle) and scikit-learn is a 30MB dependency
for one function, on top of a torch install that is already several gigabytes.
The cost of the simple rule is that it is order-dependent, which is precisely
what the second pass and the fixed ordering below are for.

**Stability.** Re-clustering happens after every index run, and it recomputes
from scratch — but a person the user has *named*, or two clusters they have
*merged*, must survive that. Neither of those facts is derivable from the
vectors, so they are anchored to something that is: a person's centroid. A fresh
cluster whose centroid is very close to a previous person's *is* that person, and
inherits their id, their name and any merge they were part of. That is what
makes `persons.id` stable enough to put in a URL and to write a name against.
"""

import numpy as np

# Fewest faces that make a person.
#
# Below this, a "person" is one or two photographs of a stranger in the
# background of a street scene — a card in the People view, with a name field,
# for somebody the user has never met. Those faces are not deleted: they keep
# their row and their vector, and simply belong to no person (cluster_id NULL),
# so a later run that finds a third one promotes them all at once.
MIN_CLUSTER = 3

# How close two faces must be to be the same person, as cosine similarity
# between unit vectors.
#
# Measured, not guessed. On the reference library — 86 photographs, 67 detected
# faces, 2,211 pairs — the distribution of vggface2 similarity is p50 0.13,
# p90 0.49, and the same person across five years, two file formats, a moustache
# and a pair of sunglasses ran 0.59–1.00 (median 0.81 inside a cluster of eleven
# that was checked by eye, crop by crop).
#
# The thresholds either side were tried on the same faces and looked at:
#
#   * 0.60 → 7 clusters, and the largest of them (14 faces) had swallowed a
#     second man and a face-painted stranger along with the eleven that belong
#     together. One card, three people, and a name field over the top of them.
#   * 0.65 → 5 clusters; those two men stay apart (11 and 6) and no cluster
#     contains a face that does not belong to it.
#   * 0.70 → the same 5 people, with one cluster shedding a member into the
#     unclustered tail. Nothing gained, one sighting lost.
#
# The asymmetry in that list is the whole argument for erring high: the cost of
# joining two people is a wrong name on somebody's photographs, which the user
# has to notice before they can undo; the cost of splitting one person in two is
# two cards they merge in one press.
JOIN = 0.65

# How close a fresh cluster's centroid must be to a previous person's for it to
# *be* that person. Higher than JOIN, and for a different job: JOIN decides
# whether two faces are one person, this decides whether two runs are talking
# about the same one. A centroid is an average of several faces, so it moves very
# little between runs (a photo added, a photo deleted) — measured drift on the
# reference library after adding six photographs was under 0.02 — while two
# genuinely different people's centroids stay far below this. Measured on the
# reference library: removing two of one person's eleven faces and re-clustering
# moved their centroid by 0.003 (1 − cos), so 0.8 leaves two orders of magnitude
# of headroom for real churn — while the *closest two different people* in that
# library are 0.49 apart, nowhere near it. The asymmetry is intentional: a false
# match here moves a name onto the wrong person.
SAME_PERSON = 0.8

# Assignment passes. The first pass's clusters depend on the order the faces
# arrive in; the second re-assigns every face against the centroids that pass
# produced, which is what removes most of that dependence. A third pass changed
# nothing on the reference library, so there is no third pass.
PASSES = 2


def _f32(mat):
    return np.asarray(mat, dtype=np.float32)


def centroid(vecs):
    """The unit-length mean of some face vectors — what a cluster *is*, for
    every comparison below.

    Re-normalized, because cosine similarity against it is a bare dot product
    and a short centroid would score low against every one of its own members. A
    degenerate mean (vectors that cancel out) keeps its zero length rather than
    being divided by ~0: it then matches nothing, which is the honest answer for
    a group with no direction."""
    v = _f32(vecs).mean(axis=0)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-8 else v


def cluster(ids, mat, join: float = JOIN, min_size: int = MIN_CLUSTER,
            passes: int = PASSES) -> dict:
    """`{face id: cluster label}` — label None for a face in no cluster.

    `ids` and `mat` are the faces matrix as stored (row i is the vector for
    ids[i]). Labels are integers from 0 and mean nothing outside this call:
    turning them into stable person ids is `assign_persons`' job.

    The faces are walked in id order, which is insertion order, which is the
    order the indexer scanned the library in — a fixed, reproducible sequence
    that does not depend on dict iteration or on which rows happened to be
    re-detected this run. Reproducibility here is not tidiness: the stability
    test reclusters the same vectors twice and expects the same answer."""
    ids = [int(i) for i in ids]
    if not ids:
        return {}
    mat = _f32(mat)
    order = sorted(range(len(ids)), key=lambda n: ids[n])

    members = []                       # [[row index, …]] per cluster
    for _ in range(max(1, passes)):
        cents = ([centroid(mat[m]) for m in members] if members else [])
        members = [[] for _ in cents]
        for n in order:
            v = mat[n]
            best, best_sim = -1, -1.0
            for c, cen in enumerate(cents):
                sim = float(v @ cen)
                if sim > best_sim:
                    best, best_sim = c, sim
            if best >= 0 and best_sim >= join:
                members[best].append(n)
                # The centroid moves as the cluster grows, within the pass: a
                # leader that never updates makes the first face of a cluster
                # its permanent definition, and a run of photographs of one
                # person then splits on whichever crop happened to be first.
                cents[best] = centroid(mat[members[best]])
            else:
                members.append([n])
                cents.append(v.copy())

    out = {}
    label = 0
    for m in members:
        if len(m) < min_size:
            for n in m:
                out[ids[n]] = None       # a face, in nobody's cluster
            continue
        for n in m:
            out[ids[n]] = label
        label += 1
    return out


def _resolve(pid, merged_into, seen=None):
    """Follow a merge chain to the person that survived it.

    Merges compose — A absorbed into B, B later absorbed into C — and the chain
    is walked rather than flattened at write time so that a merge can be
    recorded with one UPDATE. Cycle-guarded: a corrupted table (or a merge
    written the wrong way round) must not spin here forever, and stopping early
    means the worst case is a person that does not fold into another, never a
    hung index run."""
    seen = seen or set()
    while pid in merged_into and merged_into[pid] is not None:
        if pid in seen:
            break
        seen.add(pid)
        nxt = merged_into[pid]
        if nxt == pid:
            break
        pid = nxt
    return pid


def assign_persons(labels, ids, mat, prev=None):
    """Turn cluster labels into stable person ids.

    `labels` is `cluster()`'s answer, `prev` the persons already in the catalog:
    `[{"id", "name", "centroid" (np array or None), "merged_into"}]`. Returns
    `(persons, face_person)`:

      * `persons` — `[{"id", "name", "cover_face_id", "centroid"}]`, one per
        person that exists after this run. Names come across from the matched
        previous person; a new person has none until somebody types one.
      * `face_person` — `{face id: person id or None}`, exactly the faces in
        `labels` (so a face that fell out of every cluster is explicitly set
        back to None rather than left pointing at last run's person).

    Matching is greedy over all (cluster, previous person) pairs above
    SAME_PERSON, best first, one previous person to one cluster. Greedy rather
    than optimal because the alternative (Hungarian assignment) needs scipy for a
    difference that cannot arise: two previous persons that both match one new
    cluster above 0.8 are two people who are more similar to each other than a
    person is to themselves between runs, and if that ever happens the merge the
    user is about to perform is the correct answer anyway.

    Two clusters can resolve to the same person — that is what a merge *is*, on
    the next run: both of the merged-away centroids still match, and both chains
    end at the survivor. They are folded together here rather than fought over,
    which is what makes a merge survive re-clustering.
    """
    prev = list(prev or [])
    pos = {int(i): n for n, i in enumerate(int(x) for x in ids)}
    mat = _f32(mat)

    groups = {}                              # label → [face id, …]
    for fid, label in labels.items():
        if label is not None:
            groups.setdefault(label, []).append(int(fid))
    for faces in groups.values():
        faces.sort()

    cents = {label: centroid(mat[[pos[f] for f in faces]])
             for label, faces in groups.items() if all(f in pos for f in faces)}

    merged_into = {int(p["id"]): (None if p.get("merged_into") is None
                                  else int(p["merged_into"])) for p in prev}
    by_id = {int(p["id"]): p for p in prev}

    pairs = []
    for label, cen in cents.items():
        for p in prev:
            pc = p.get("centroid")
            if pc is None or not len(pc):
                continue
            sim = float(cen @ _f32(pc))
            if sim >= SAME_PERSON:
                pairs.append((sim, label, int(p["id"])))
    # sorted by similarity, then by ids, so two equal similarities resolve the
    # same way on every run rather than by list order
    pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
    taken_label, taken_person = {}, set()
    for sim, label, pid in pairs:
        if label in taken_label or pid in taken_person:
            continue
        taken_label[label] = pid
        taken_person.add(pid)

    next_id = max([int(p["id"]) for p in prev] or [0]) + 1
    resolved = {}                            # label → surviving person id
    for label in sorted(groups):
        matched = taken_label.get(label)
        if matched is None:
            resolved[label] = next_id
            next_id += 1
        else:
            resolved[label] = _resolve(matched, merged_into)

    persons, face_person = {}, {}
    for label, faces in sorted(groups.items()):
        pid = resolved[label]
        bucket = persons.setdefault(
            pid, {"id": pid, "name": None, "faces": [], "labels": []})
        bucket["faces"].extend(faces)
        bucket["labels"].append(label)
        for f in faces:
            face_person[f] = pid
    for fid, label in labels.items():
        if label is None:
            face_person[int(fid)] = None

    out = []
    for pid, bucket in sorted(persons.items()):
        faces = sorted(bucket["faces"])
        cen = centroid(mat[[pos[f] for f in faces]])
        # The name survives from whichever matched person carried one. Read
        # through the *matched* rows rather than through `pid`, because a merged
        # person's name may live on the row that was absorbed.
        name = None
        for label in bucket["labels"]:
            src = taken_label.get(label)
            for candidate in ({src} if src is not None else set()) | {pid}:
                row = by_id.get(candidate)
                if row and row.get("name") and not name:
                    name = row["name"]
        out.append({"id": pid, "name": name,
                    # The cover is the face closest to the centre of the cluster
                    # — the most typical picture of this person rather than the
                    # first one indexed, which is as likely to be a profile at
                    # the edge of a group shot.
                    "cover_face_id": max(
                        faces, key=lambda f: (float(mat[pos[f]] @ cen), -f)),
                    "centroid": cen, "face_count": len(faces)})
    return out, face_person


def seed_names(persons, face_person, votes, min_votes: int = MIN_CLUSTER):
    """`{person id: name}` for people Apple Photos has effectively already named.

    `votes` is `{face id: name}` — built by the caller from the Photos library's
    own `persons` list, and only for photographs with **exactly one** detected
    face, because that is the only case where "this library says Ana is in this
    photo" can be attached to a particular face without guessing.

    A name is taken only when the cluster agrees with itself `min_votes` times
    and no other name gets as many. One agreeing photograph is a coincidence;
    three is the library telling us something. And an already-named person is
    never overwritten: a name the user typed outranks anything inferred.
    """
    tally = {}
    for fid, name in votes.items():
        pid = face_person.get(int(fid))
        name = (name or "").strip()
        if pid is None or not name:
            continue
        tally.setdefault(pid, {})
        tally[pid][name] = tally[pid].get(name, 0) + 1
    out = {}
    for p in persons:
        if p.get("name"):
            continue
        counts = tally.get(p["id"])
        if not counts:
            continue
        ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        (name, n) = ranked[0]
        if n < min_votes:
            continue
        # An outright tie is two names with equal evidence, and picking one
        # alphabetically would be inventing a fact. Leave it for a human.
        if len(ranked) > 1 and ranked[1][1] == n:
            continue
        out[p["id"]] = name
    return out
