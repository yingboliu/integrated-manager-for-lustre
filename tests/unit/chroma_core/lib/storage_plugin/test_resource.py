from tests.unit.lib.iml_unit_test_case import IMLUnitTestCase
from chroma_core.lib.storage_plugin.api import attributes, statistics
from chroma_core.lib.storage_plugin.api.identifiers import GlobalId
from chroma_core.lib.storage_plugin.base_resource import BaseStorageResource


class TestDefaults1(BaseStorageResource):
    class Meta:
        identifier = GlobalId("name")

    name = attributes.String()
    bytes = statistics.BytesHistogram(bins=[(0, 100)])


class TestDefaults2(BaseStorageResource):
    class Meta:
        identifier = GlobalId("name", "name_scope")

    name = attributes.String()
    name_scope = attributes.String()
    read = statistics.Gauge()
    write = statistics.Gauge()


class TestOverrides(BaseStorageResource):
    class Meta:
        identifier = GlobalId("name")
        label = "Alpha"
        charts = [{"title": "IO", "series": ["read", "write"]}]

    def get_label(self):
        return "Bravo"

    name = attributes.String()
    read = statistics.Gauge()
    write = statistics.Gauge()


class TestDefaultAndOptional(BaseStorageResource):
    class Meta:
        identifier = GlobalId("name")

    name = attributes.String()
    name_not_optional_not_default = attributes.String()
    name_optional = attributes.String(optional=True)
    name_optional_trumps_default = attributes.String(optional=True, default="never used")
    name_default_value = attributes.String(default="default value")
    name_default_callable = attributes.String(default=lambda storage_dict: "default callable")
    name_default_callable_bob = attributes.String(default=lambda storage_dict: storage_dict["name"])


class TestDisplayNames(IMLUnitTestCase):
    def test_defaults(self):
        td1 = TestDefaults1(name="foo")
        self.assertEqual(td1.get_label(), "TestDefaults1 foo")
        self.assertRaises(ValueError, setattr, td1, "bytes", [])

        td2 = TestDefaults2(name="foo", name_scope="bar")
        self.assertEqual(td2.get_label(), "TestDefaults2 ('foo', 'bar')")
        self.assertEqual(len(td2.get_charts()), 2)
        td2.read = 0.0
        with self.assertRaises(ValueError):
            td2.write = ""

    def test_overrides(self):
        to = TestOverrides(name="foo")
        self.assertEqual(to.get_label(), "Bravo")
        self.assertEqual(to._meta.label, "Alpha")
        self.assertEqual(len(to.get_charts()), 1)

    def test_default_and_optional(self):
        """Test the default works for the cases of None, callable and value"""
        test_defaults_and_optional = TestDefaultAndOptional(name="Bob")

        self.assertEqual(test_defaults_and_optional.name, "Bob")
        self.assertRaises(AttributeError, lambda: test_defaults_and_optional.name_not_optional_not_default)
        self.assertEqual(test_defaults_and_optional.name_optional, None)
        self.assertEqual(test_defaults_and_optional.name_optional_trumps_default, None)
        self.assertEqual(test_defaults_and_optional.name_default_value, "default value")
        self.assertEqual(test_defaults_and_optional.name_default_callable, "default callable")
        self.assertEqual(test_defaults_and_optional.name_default_callable_bob, "Bob")


class TestDeltaChanges(IMLUnitTestCase):
    def test_delta_changes(self):
        """Test changes are recorded in _delta when they occur."""

        test_delta_changes = TestDefaults1(name="Bob")

        test_delta_changes.name = "Freddie"
        self.assertEqual(test_delta_changes._delta_attrs, {"name": "Freddie"})

        # Reset and it should not set again with the same value.
        test_delta_changes._delta_attrs = {}
        test_delta_changes.name = "Freddie"
        self.assertEqual(test_delta_changes._delta_attrs, {})

        # Reset and it should set again with a  different value.
        test_delta_changes._delta_attrs = {}
        test_delta_changes.name = "Charlie"
        self.assertEqual(test_delta_changes._delta_attrs, {"name": "Charlie"})

    def test_no_delta_changes(self):
        """Test changes are recorded in _delta when they occur."""

        test_delta_changes = TestDefaults1(name="Bob", calc_changes_delta=lambda: False)

        test_delta_changes.name = "Freddie"
        self.assertEqual(test_delta_changes._delta_attrs, {"name": "Freddie"})

        # Reset and it should set again even with the same value.
        test_delta_changes._delta_attrs = {}
        test_delta_changes.name = "Freddie"
        self.assertEqual(test_delta_changes._delta_attrs, {"name": "Freddie"})

        # Reset and it should set again with a  different value.
        test_delta_changes._delta_attrs = {}
        test_delta_changes.name = "Charlie"
        self.assertEqual(test_delta_changes._delta_attrs, {"name": "Charlie"})
