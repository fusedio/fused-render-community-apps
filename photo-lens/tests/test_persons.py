"""Clustering, and the two user facts that have to survive it: names and merges.

Every vector here is written by hand. That is deliberate — these are tests of the
*rules*, and a rule that groups four synthetic points is the same rule that
groups four faces (see tests/conftest.py on why identity is expressed as
geometry rather than as photographs of people).
"""
import numpy as np

from lens import persons


def _unit(*xs):
    v = np.asarray(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


def _near(base, tilt, axis=2, dim=4):
    """A vector `tilt` off `base`'s axis — a second photograph of one person."""
    v = np.zeros(dim, dtype=np.float32)
    v[base] = 1.0
    v[axis] = tilt
    return v / np.linalg.norm(v)


def _matrix(vecs):
    return (np.arange(1, len(vecs) + 1, dtype=np.int64),
            np.stack(vecs).astype(np.float16))


def test_cluster_groups_the_close_and_leaves_the_lonely_alone():
    """Three sightings make a person; one does not.

    The single face is not an error and is not deleted — it comes back as
    `None`, which is what the catalog stores and what promotes it the moment a
    third sighting of the same face turns up."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3),
                        _near(1, 0.0), _near(1, 0.1), _near(1, 0.2),
                        _unit(0, 0, 0, 1)])
    labels = persons.cluster(ids, mat)

    groups = {}
    for fid, label in labels.items():
        groups.setdefault(label, []).append(fid)
    assert sorted(groups[labels[1]]) == [1, 2, 3]
    assert sorted(groups[labels[4]]) == [4, 5, 6]
    assert labels[1] != labels[4]
    assert labels[7] is None                      # one face, nobody yet


def test_cluster_is_a_threshold_not_an_equality():
    """Two crops of one person are never identical vectors, so the rule has to
    hold them together across a real gap — and still refuse a pair that is
    merely closer to each other than to anything else."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.6), _near(0, 0.55),
                        # 0.55/0.6 off-axis is ~0.86 similarity: same person.
                        # These two are 45° from everything, and from each other.
                        _unit(0, 1, 1, 0), _unit(0, 1, 0, 1), _unit(0, 0, 1, 1)])
    labels = persons.cluster(ids, mat)
    assert labels[1] == labels[2] == labels[3] is not None
    # the mutually-distant three are one cluster only if the threshold is a lie
    assert len({labels[4], labels[5], labels[6]}) > 1 or labels[4] is None


def test_reclustering_the_same_vectors_gives_the_same_people_and_keeps_names():
    """The stability contract, stated as a test: re-clustering is a recompute,
    and a recompute must not rename anybody.

    This is what makes `persons.id` safe to put in a URL and to write a name
    against — without it, every index run would shuffle the People view."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3),
                        _near(1, 0.0), _near(1, 0.1), _near(1, 0.2)])
    labels = persons.cluster(ids, mat)
    first, face_person = persons.assign_persons(labels, ids, mat, [])
    assert [p["id"] for p in first] == [1, 2]

    # the user names one of them
    prev = [{"id": p["id"], "name": "Ana" if p["id"] == 1 else None,
             "centroid": p["centroid"], "merged_into": None} for p in first]

    again, face_again = persons.assign_persons(
        persons.cluster(ids, mat), ids, mat, prev)
    assert [p["id"] for p in again] == [1, 2]
    assert [p["name"] for p in again] == ["Ana", None]
    assert face_again == face_person
    # ...and the covers did not wander either: same faces, same centre
    assert [p["cover_face_id"] for p in again] == [p["cover_face_id"] for p in first]


def test_a_new_face_joins_an_existing_person_rather_than_founding_one():
    """A photograph added between two runs is the common case, and it must not
    look like a new person: the centroid barely moves, so the match holds."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3)])
    people, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, [])
    prev = [{"id": people[0]["id"], "name": "Ana",
             "centroid": people[0]["centroid"], "merged_into": None}]

    ids2 = np.array([1, 2, 3, 4], dtype=np.int64)
    mat2 = np.stack([mat[0], mat[1], mat[2], _near(0, 0.25)]).astype(np.float16)
    again, face_person = persons.assign_persons(
        persons.cluster(ids2, mat2), ids2, mat2, prev)

    assert [p["id"] for p in again] == [1]
    assert again[0]["name"] == "Ana" and again[0]["face_count"] == 4
    assert face_person[4] == 1


def test_a_merge_survives_reclustering():
    """Two clusters the user merged must not split apart again on the next index
    run — and nothing in the vectors says they are one person, so the merged-away
    row's centroid is what carries the decision (store.merge_persons keeps it).
    """
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3),
                        _near(1, 0.0), _near(1, 0.1), _near(1, 0.2)])
    people, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, [])
    a, b = people[0], people[1]

    # …as the catalog looks after merging b into a: b keeps its centroid and
    # points at a, and a carries the name
    prev = [{"id": a["id"], "name": "Ana", "centroid": a["centroid"],
             "merged_into": None},
            {"id": b["id"], "name": None, "centroid": b["centroid"],
             "merged_into": a["id"]}]

    again, face_person = persons.assign_persons(
        persons.cluster(ids, mat), ids, mat, prev)

    assert [p["id"] for p in again] == [a["id"]]
    assert again[0]["name"] == "Ana"
    assert again[0]["face_count"] == 6                # both clusters, one person
    assert set(face_person.values()) == {a["id"]}


def test_a_merge_chain_resolves_to_the_survivor():
    """A absorbed into B, later absorbed into C: a face that matches A's old
    centroid belongs to C, not to a row that no longer exists."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3)])
    people, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, [])
    cen = people[0]["centroid"]
    prev = [{"id": 1, "name": None, "centroid": cen, "merged_into": 2},
            {"id": 2, "name": None, "centroid": cen, "merged_into": 3},
            {"id": 3, "name": "Ana", "centroid": cen, "merged_into": None}]

    again, face_person = persons.assign_persons(
        persons.cluster(ids, mat), ids, mat, prev)
    assert [p["id"] for p in again] == [3]
    assert again[0]["name"] == "Ana"
    assert set(face_person.values()) == {3}


def test_a_merge_cycle_does_not_hang():
    """A corrupt table must cost a wrong-looking person, never an index run that
    never finishes."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3)])
    people, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, [])
    cen = people[0]["centroid"]
    prev = [{"id": 1, "name": None, "centroid": cen, "merged_into": 2},
            {"id": 2, "name": None, "centroid": cen, "merged_into": 1}]
    again, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, prev)
    assert len(again) == 1


def test_a_person_who_lost_their_faces_does_not_come_back_as_a_stranger():
    """An id is not reused. The row survives a library that no longer contains
    the person (an unplugged drive), so the next id must go past it — otherwise
    reconnecting the drive would find their photographs filed under somebody
    else's name."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3)])
    prev = [{"id": 7, "name": "Ana", "centroid": _unit(0, 0, 1, 0),
             "merged_into": None}]
    people, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, prev)
    assert [p["id"] for p in people] == [8]
    assert people[0]["name"] is None


def test_the_cover_is_the_most_typical_face_not_the_first():
    """The cover is what a card shows, so it is the face closest to the middle of
    the cluster rather than whichever one was indexed first — which is as likely
    to be a profile at the edge of a group shot."""
    # two faces taken a moment apart, and one three-quarter profile: the middle
    # of the cluster is between the first two, and the outlier is not it
    ids, mat = _matrix([_near(0, 0.20), _near(0, 0.22), _near(0, 0.90)])
    people, _ = persons.assign_persons(persons.cluster(ids, mat), ids, mat, [])
    assert people[0]["cover_face_id"] == 2


def test_seed_names_needs_agreement_and_never_overwrites():
    """A name from the Photos library is evidence, not instruction: three
    agreeing photographs take it, two do not, a tie takes neither, and a name the
    user typed is never touched."""
    people = [{"id": 1, "name": None}, {"id": 2, "name": None},
              {"id": 3, "name": "Typed"}, {"id": 4, "name": None}]
    face_person = {10: 1, 11: 1, 12: 1,        # three agreeing
                   20: 2, 21: 2,               # two agreeing
                   30: 3, 31: 3, 32: 3,        # three, but already named
                   40: 4, 41: 4, 42: 4}        # three, disagreeing 2–1… and a tie
    votes = {10: "Ana", 11: "Ana", 12: "Ana",
             20: "Ben", 21: "Ben",
             30: "Cleo", 31: "Cleo", 32: "Cleo",
             40: "Dee", 41: "Dee", 42: "Dee"}
    assert persons.seed_names(people, face_person, votes) == {1: "Ana", 4: "Dee"}

    # …and a real tie: three photographs say Dee, three say Eve. Picking either
    # would be inventing a fact, so the card keeps asking to be named.
    tie = {**votes, 43: "Eve", 44: "Eve", 45: "Eve"}
    face_person = {**face_person, 43: 4, 44: 4, 45: 4}
    assert persons.seed_names(people, face_person, tie) == {1: "Ana"}


def test_a_face_in_nobody_is_written_back_as_nobody():
    """A face that fell out of every cluster this run must be *set* to None, not
    left alone: left alone it would keep pointing at last run's person, and that
    assignment is no longer supported by anything."""
    ids, mat = _matrix([_near(0, 0.0), _near(0, 0.2), _near(0, 0.3),
                        _unit(0, 0, 0, 1)])
    labels = persons.cluster(ids, mat)
    _, face_person = persons.assign_persons(labels, ids, mat, [])
    assert face_person[4] is None
    assert set(face_person) == {1, 2, 3, 4}


def test_clustering_an_empty_library_is_not_an_error():
    ids = np.zeros((0,), dtype=np.int64)
    mat = np.zeros((0, 4), dtype=np.float16)
    assert persons.cluster(ids, mat) == {}
    people, mapping = persons.assign_persons({}, ids, mat, [])
    assert people == [] and mapping == {}
