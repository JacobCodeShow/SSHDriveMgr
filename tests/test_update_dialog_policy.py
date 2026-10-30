"""Update UI handover policy with simulated Qt signals; no GUI/network/install."""
import ast
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[1] / "src/ui/dialogs/update_dialog.py"


def definition(name):
    tree = ast.parse(PATH.read_text(encoding="utf-8"))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    node.decorator_list = []
    module = ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), node], type_ignores=[])
    ns = {"tr": lambda key, **kw: key}
    exec(compile(ast.fix_missing_locations(module), str(PATH), "exec"), ns)
    return ns[name]


class HandoverDialogTests(unittest.TestCase):
    def run_callback(self, success, record=True):
        qt = types.ModuleType("PyQt6")
        core = types.ModuleType("PyQt6.QtCore")
        widgets = types.ModuleType("PyQt6.QtWidgets")
        core.QTimer = mock.Mock()
        widgets.QApplication = mock.Mock()
        policy = types.ModuleType("src.updater")
        policy.launch_pending_installer = mock.Mock(return_value=success)
        policy.set_install_on_next_start = mock.Mock(return_value=True)
        policy.get_update_handover_error = mock.Mock(return_value="Verified update rejected")
        messages = types.ModuleType("src.ui.dialogs.styled_message_box")
        messages.StyledMessageBox = mock.Mock()
        updater = mock.Mock(current_version="1.0.0")
        updater.pending_update.return_value = {"version": "1.1.0"} if record else None
        dialog = mock.Mock()
        modules = {"PyQt6": qt, "PyQt6.QtCore": core, "PyQt6.QtWidgets": widgets,
                   "src.updater": policy, "src.ui.dialogs.styled_message_box": messages}
        with mock.patch.dict(sys.modules, modules):
            definition("_wire")(dialog, updater)
            dialog.install_now.connect.call_args.args[0]()
        return dialog, policy, messages.StyledMessageBox, core.QTimer

    def test_failed_or_elevated_handover_keeps_dialog_open_and_reports_reason(self):
        dialog, policy, messages, timer = self.run_callback(False)
        dialog.accept.assert_not_called()
        dialog.install_btn.setEnabled.assert_called_with(True)
        messages.warning.assert_called_once()
        self.assertIn("Verified update rejected", messages.warning.call_args.args)
        timer.singleShot.assert_not_called()

    def test_verified_success_closes_dialog_and_schedules_shutdown(self):
        dialog, policy, messages, timer = self.run_callback(True)
        policy.launch_pending_installer.assert_called_once()
        dialog.accept.assert_called_once()
        timer.singleShot.assert_called_once()
        messages.warning.assert_not_called()

    def test_missing_verified_payload_does_not_attempt_execution(self):
        dialog, policy, messages, timer = self.run_callback(False, record=False)
        policy.launch_pending_installer.assert_not_called()
        dialog.accept.assert_not_called()
        messages.warning.assert_called_once()

    def test_install_button_alone_cannot_claim_handover_success(self):
        dialog = mock.Mock()
        definition("_restart_and_install")(dialog)
        dialog.install_now.emit.assert_called_once()
        dialog.accept.assert_not_called()


if __name__ == "__main__":
    unittest.main()
