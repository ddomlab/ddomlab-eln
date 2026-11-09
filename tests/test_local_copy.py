"""Tests against a real ELN on this computer. See conftest.py for how to run them."""
import datetime
import json

import elabapi_python
import pytest

from ddomlab_eln import ELN, Attachment, ELNError, Experiment, Links, Template

MISSING = 999999


def raw_fields(eln, exp_id):
    return json.loads(eln._read_experiment(exp_id)["metadata"])["extra_fields"]


def values(eln, exp_id):
    return {name: field["value"] for name, field in raw_fields(eln, exp_id).items()}


# --- Connecting


def test_connects_and_knows_who_you_are(eln):
    assert eln.user.fullname in repr(eln)


def test_wrong_key_is_refused(eln):
    with pytest.raises(ELNError, match="doesn't accept this API key"):
        ELN(url=eln.url, api_key="wrong")


# --- Templates


def test_get_template_keeps_settings_and_order(eln, template_id):
    template = eln.get_template(template_id)
    assert list(template.fields) == ["Sample ID", "Scan rate", "Mode"]
    assert template.fields["Sample ID"]["required"] is True
    assert template.fields["Scan rate"]["unit"] == "deg/min"
    assert template.fields["Mode"]["options"] == ["fast", "slow"]


def test_get_template_missing(eln):
    with pytest.raises(ELNError, match="no experiment template"):
        eln.get_template(MISSING)


def test_list_templates_matches_get_template(eln, template_id):
    listed = {template.id: template for template in eln.list_templates()}
    assert isinstance(listed[template_id], Template)
    assert listed[template_id] == eln.get_template(template_id)


# --- Creating


def test_create_fills_fields_and_keeps_template_settings(eln, make_experiment):
    exp_id = make_experiment(Scan_rate=2, Mode="slow")
    fields = raw_fields(eln, exp_id)
    assert fields["Sample ID"]["value"] == "S-1" and fields["Sample ID"]["required"] is True
    assert fields["Mode"]["value"] == "slow" and fields["Mode"]["options"] == ["fast", "slow"]


@pytest.mark.parametrize("fields, message", [
    ({"Sampel ID": "x", "Sample ID": "y"}, 'Did you mean "Sample ID"'),
    ({"Scan rate": 2}, '"Sample ID" is required'),
    ({"Sample ID": "x", "Scan rate": "fast"}, "expects a number"),
    ({"Sample ID": "x", "Mode": "medium"}, "invalid option"),
])
def test_create_refuses_bad_fields(eln, template_id, token, fields, message):
    with pytest.raises(ELNError, match=message):
        eln.create_experiment(template_id, f"{token} refused", fields)
    assert eln.find_experiments(f"{token} refused") == []


def test_create_with_missing_template(eln):
    with pytest.raises(ELNError, match="no experiment template"):
        eln.create_experiment(MISSING, "x", {})


# --- Updating fields


def test_update_fields(eln, make_experiment):
    exp_id = make_experiment()
    eln.update_fields(exp_id, {"Scan rate": 3, "Mode": "slow"})
    assert eln.get_experiment(exp_id).fields["Scan rate"] == 3
    assert values(eln, exp_id)["Mode"] == "slow"


def test_update_is_all_or_nothing(eln, make_experiment):
    exp_id = make_experiment()
    with pytest.raises(ELNError, match="expects a number"):
        eln.update_fields(exp_id, {"Sample ID": "changed", "Scan rate": "fast"})
    assert values(eln, exp_id)["Sample ID"] == "S-1"


@pytest.mark.parametrize("fields, message", [
    ({"Sampel ID": "x"}, "Did you mean"),
    ({"Sample ID": ""}, "is required"),
    ({"Mode": "medium"}, "invalid option"),
])
def test_update_refuses_bad_fields_and_changes_nothing(eln, make_experiment, fields, message):
    exp_id = make_experiment()
    before = values(eln, exp_id)
    with pytest.raises(ELNError, match=message):
        eln.update_fields(exp_id, fields)
    assert values(eln, exp_id) == before


def test_update_missing_experiment(eln):
    with pytest.raises(ELNError, match="no experiment"):
        eln.update_fields(MISSING, {"x": 1})


# --- Main text


def test_add_note_appends_and_escapes(eln, make_experiment):
    exp_id = make_experiment()
    eln.add_note(exp_id, "first")
    eln.add_note(exp_id, "a < b\nline two")
    assert eln.get_experiment(exp_id).main_text == "<p>first</p><p>a &lt; b<br>line two</p>"


def test_set_text_writes_to_an_empty_experiment(eln, make_experiment):
    exp_id = make_experiment()
    eln.set_text(exp_id, "report")
    assert eln.get_experiment(exp_id).main_text == "<p>report</p>"


def test_set_text_protects_existing_text(eln, make_experiment):
    exp_id = make_experiment()
    eln.add_note(exp_id, "typed by hand")
    with pytest.raises(ELNError, match="already has main text"):
        eln.set_text(exp_id, "new")
    assert "typed by hand" in eln.get_experiment(exp_id).main_text
    eln.set_text(exp_id, "new", overwrite=True)
    assert eln.get_experiment(exp_id).main_text == "<p>new</p>"


# --- Files


def test_upload_lists_the_file(eln, make_experiment, tmp_path):
    exp_id = make_experiment()
    path = tmp_path / "run 2θ.xy"
    path.write_text("1,2\n", encoding="utf-8")
    upload_id = eln.upload_file(exp_id, path, comment="raw")
    assert eln.get_experiment(exp_id).files == [Attachment(id=upload_id, name="run 2θ.xy", comment="raw", size=4)]


def test_upload_skips_an_identical_file_but_keeps_a_changed_one(eln, make_experiment, tmp_path):
    exp_id = make_experiment()
    path = tmp_path / "run.xy"
    path.write_text("1,2\n")
    first = eln.upload_file(exp_id, str(path))
    assert eln.upload_file(exp_id, path) == first
    assert len(eln.get_experiment(exp_id).files) == 1
    path.write_text("3,4\n")
    assert eln.upload_file(exp_id, path) != first
    assert len(eln.get_experiment(exp_id).files) == 2


def test_upload_refuses_missing_file_and_folder(eln, make_experiment, tmp_path):
    exp_id = make_experiment()
    for path in (tmp_path / "missing.xy", tmp_path):
        with pytest.raises(ELNError, match="There is no file"):
            eln.upload_file(exp_id, path)


# --- Links


def test_find_resources(eln, resource_id, token):
    found = eln.find_resources(token.upper())
    assert [resource.id for resource in found] == [resource_id]
    assert eln.find_resources(f"{token}nothing") == []


def test_link_resource_twice_is_one_link(eln, make_experiment, resource_id, token):
    exp_id = make_experiment()
    assert eln.link_resource(exp_id, resource_id) == f"{token} resource"
    eln.link_resource(exp_id, resource_id)
    assert [resource.id for resource in eln.get_links(exp_id).resources] == [resource_id]


def test_link_errors_name_the_missing_id(eln, make_experiment, resource_id):
    exp_id = make_experiment()
    with pytest.raises(ELNError, match=f"no resource #{MISSING}"):
        eln.link_resource(exp_id, MISSING)
    with pytest.raises(ELNError, match=f"no experiment #{MISSING}"):
        eln.link_resource(MISSING, resource_id)
    with pytest.raises(ELNError, match=f"no experiment #{MISSING}"):
        eln.link_experiment(exp_id, MISSING)


def test_link_experiment_shows_on_both_sides(eln, make_experiment):
    first, second = make_experiment("first"), make_experiment("second")
    assert eln.link_experiment(first, second).endswith("second")
    links = eln.get_links(first)
    assert isinstance(links, Links) and [link.id for link in links.experiments] == [second]
    assert [link.id for link in eln.get_links(second).linked_from] == [first]


def test_link_to_itself_is_refused(eln, make_experiment):
    exp_id = make_experiment()
    with pytest.raises(ELNError, match="to itself"):
        eln.link_experiment(exp_id, exp_id)


def test_get_links_leaves_out_the_trash(eln, make_experiment):
    first, second = make_experiment("first"), make_experiment("second")
    eln.link_experiment(first, second)
    elabapi_python.ExperimentsApi(eln.client).delete_experiment(second)
    assert eln.get_links(first).experiments == []


# --- Tags


def test_add_tags_one_or_several(eln, make_experiment):
    exp_id = make_experiment()
    eln.add_tags(exp_id, "pytest-tag-one")
    eln.add_tags(exp_id, ["pytest-tag-two", "pytest-tag-one"])
    assert sorted(eln.get_experiment(exp_id).tags) == ["pytest-tag-one", "pytest-tag-two"]


def test_empty_tag_refuses_the_whole_list(eln, make_experiment):
    exp_id = make_experiment()
    with pytest.raises(ELNError, match="can't be empty"):
        eln.add_tags(exp_id, ["pytest-tag-one", "  "])
    assert eln.get_experiment(exp_id).tags == []


# --- New fields


@pytest.mark.parametrize("value, options, stored", [
    ("hello", {}, "hello"),
    ("", {}, ""),
    ("fast", {"field_type": "select", "options": ["fast", "slow"]}, "fast"),
    ("2026-11-01", {"field_type": "date"}, "2026-11-01"),
    (datetime.date(2026, 11, 1), {"field_type": "date"}, "2026-11-01"),
    (True, {"field_type": "checkbox"}, "on"),
    (False, {"field_type": "checkbox"}, ""),
])
def test_add_field(eln, make_experiment, value, options, stored):
    exp_id = make_experiment()
    eln.add_field(exp_id, "New one", value, **options)
    assert raw_fields(eln, exp_id)["New one"]["value"] == stored
    assert values(eln, exp_id)["Sample ID"] == "S-1"


def test_add_number_field_with_unit_goes_last(eln, make_experiment):
    exp_id = make_experiment()
    eln.add_field(exp_id, "Room temperature", 21.5, field_type="number", unit="C")
    field = raw_fields(eln, exp_id)["Room temperature"]
    assert field["unit"] == "C" and field["units"] == ["C"]
    assert field["position"] == 4  # the web page orders fields by this number
    fields = eln.get_experiment(exp_id).fields
    assert list(fields)[-1] == "Room temperature" and fields["Room temperature"] == 21.5


@pytest.mark.parametrize("name, value, options, message", [
    ("New", "warm", {"field_type": "number"}, "expects a number"),
    ("New", "z", {"field_type": "select", "options": ["x"]}, "invalid option"),
    ("New", "z", {"field_type": "select"}, "needs options"),
    ("New", "z", {"options": ["z"]}, "can have options"),
    ("New", "z", {"unit": "C"}, "can have a unit"),
    ("New", "yesterday", {"field_type": "date"}, "must be a date"),
    ("New", "maybe", {"field_type": "checkbox"}, "must be True or False"),
    ("New", 1, {"field_type": "checkbox"}, "must be True or False"),
    ("New", "x", {"field_type": "banana"}, "field_type must be"),
    ("   ", "x", {}, "needs a name"),
    ("Sample ID", "x", {}, "already has a field"),
    ("sample id", "x", {}, "already has a field"),
])
def test_add_field_refusals_change_nothing(eln, make_experiment, name, value, options, message):
    exp_id = make_experiment()
    before = raw_fields(eln, exp_id)
    with pytest.raises(ELNError, match=message):
        eln.add_field(exp_id, name, value, **options)
    assert raw_fields(eln, exp_id) == before


# --- Reading one experiment


def test_get_experiment(eln, make_experiment, template_id):
    exp_id = make_experiment(Scan_rate=2)
    eln.add_note(exp_id, "Hello <world>")
    exp = eln.get_experiment(exp_id)
    assert isinstance(exp, Experiment)
    assert exp.template_id == template_id and exp.owner == eln.user.fullname
    assert exp.fields["Scan rate"] == 2 and isinstance(exp.fields["Scan rate"], int)
    assert exp.main_text == "<p>Hello &lt;world&gt;</p>" and exp.first_line == "Hello <world>"


def test_get_experiment_missing(eln):
    with pytest.raises(ELNError, match="no experiment"):
        eln.get_experiment(MISSING)


# --- Searching


@pytest.fixture
def three_runs(make_experiment):
    return (
        make_experiment("alpha one", **{"Sample ID": "S-1"}, Scan_rate=2, Mode="slow"),
        make_experiment("beta", **{"Sample ID": "S-12"}, Scan_rate=12),
        make_experiment("gamma", **{"Sample ID": "S-123"}, Scan_rate=2.5),
    )


def ids(experiments):
    return [experiment.id for experiment in experiments]


def test_find_with_no_options_is_only_mine_and_newest_first(eln, three_runs):
    found = eln.find_experiments(limit=100)
    assert {experiment.owner for experiment in found} == {eln.user.fullname}
    assert ids(found) == sorted(ids(found), reverse=True)
    assert ids(found)[0] == three_runs[-1]


def test_find_limit(eln, three_runs):
    assert ids(eln.find_experiments(limit=2)) == [three_runs[2], three_runs[1]]


def test_find_only_mine_also_holds_during_an_extended_search(eln, three_runs):
    found = eln.find_experiments(until=datetime.date.today(), limit=100)
    assert {experiment.owner for experiment in found} == {eln.user.fullname}


def test_find_owner_all_includes_mine(eln, three_runs, token):
    assert set(three_runs) == set(ids(eln.find_experiments(token, owner="all")))


@pytest.mark.parametrize("kwargs, message", [
    ({"limit": 0}, "limit must be"),
    ({"owner": "bob"}, "owner must be"),
    ({"since": "last week"}, "must be a date"),
])
def test_find_refuses_bad_options(eln, kwargs, message):
    with pytest.raises(ELNError, match=message):
        eln.find_experiments(**kwargs)


def test_find_text_needs_every_word_in_any_order(eln, three_runs, token):
    assert ids(eln.find_experiments(f"one alpha {token}")) == [three_runs[0]]


def test_find_text_is_not_read_as_search_syntax(eln, three_runs, token):
    assert eln.find_experiments("a:b") == []
    assert eln.find_experiments(f'{token}"x') == []


def test_find_text_in_the_main_text(eln, three_runs, token):
    eln.add_note(three_runs[1], f"{token}note")
    assert ids(eln.find_experiments(f"{token}note")) == [three_runs[1]]


@pytest.mark.parametrize("wanted, expected", [
    ({"Sample ID": "S-1"}, [0]),
    ({"Sample ID": "s-12"}, [1]),
    ({"Sample ID": "S-1%"}, [2, 1, 0]),
    ({"Scan rate": 2}, [0]),
    ({"Scan rate": 2.5}, [2]),
    ({"Sample ID": "S-1", "Mode": "slow"}, [0]),
    ({"Sample ID": "S-1", "Mode": "fast"}, []),
    ({"Sample ID": "S-999"}, []),
])
def test_find_fields_match_the_whole_value(eln, three_runs, token, wanted, expected):
    assert ids(eln.find_experiments(token, fields=wanted)) == [three_runs[i] for i in expected]


def test_find_field_value_containing_a_quote(eln, make_experiment, token):
    exp_id = make_experiment(**{"Sample ID": 'Q"1'})
    assert ids(eln.find_experiments(token, fields={"Sample ID": 'Q"1'})) == [exp_id]


def test_find_dates(eln, three_runs, token):
    today = datetime.date.today()
    assert len(eln.find_experiments(token, since=today)) == 3
    assert len(eln.find_experiments(token, until=today.isoformat())) == 3
    assert eln.find_experiments(token, until=today - datetime.timedelta(days=1)) == []
    assert eln.find_experiments(token, since=today + datetime.timedelta(days=1)) == []


def test_find_template(eln, three_runs, token, template_id):
    assert set(ids(eln.find_experiments(token, template_id=template_id))) == set(three_runs)
    assert eln.find_experiments(token, template_id=MISSING) == []


def test_find_tags(eln, three_runs, token):
    eln.add_tags(three_runs[0], ["pytest-tag-one", "pytest-tag-two"])
    eln.add_tags(three_runs[1], "pytest-tag-one")
    assert ids(eln.find_experiments(token, tags="pytest-tag-one")) == [three_runs[1], three_runs[0]]
    assert ids(eln.find_experiments(token, tags=["pytest-tag-one", "pytest-tag-two"])) == [three_runs[0]]


def test_find_status_is_exact(eln, three_runs, token):
    somebody_elses = eln.find_experiments(status="Success", owner="all", limit=1)
    if not somebody_elses:
        pytest.skip('this copy has no experiment with the status "Success"')
    status_id = eln._read_experiment(somebody_elses[0].id)["status"]
    eln._patch_experiment(three_runs[0], {"status": status_id}, "set the status of")
    assert ids(eln.find_experiments(token, status="success")) == [three_runs[0]]
    assert eln.find_experiments(token, status="Suc") == []


def test_find_results_are_filled_in(eln, three_runs, template_id):
    eln.add_note(three_runs[0], "The idea")
    eln.add_tags(three_runs[0], "pytest-tag-one")
    found = eln.find_experiments(fields={"Sample ID": "S-1"}, limit=100)
    result = next(experiment for experiment in found if experiment.id == three_runs[0])
    assert result.template_id == template_id
    assert result.fields["Scan rate"] == 2 and result.first_line == "The idea" and result.tags == ["pytest-tag-one"]
