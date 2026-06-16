import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import taskhub_voice as voice  # noqa: E402


class VoiceModelResolution(unittest.TestCase):
    def test_explicit_model_wins(self):
        self.assertEqual(
            voice.resolve_whisper_model(explicit="~/model.bin", module_dir="/tmp/app", cwd="/tmp/repo"),
            os.path.expanduser("~/model.bin"),
        )

    def test_prefers_quantized_model_in_module_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            models = os.path.join(tmp, "models")
            os.makedirs(models)
            q5 = os.path.join(models, "ggml-large-v3-turbo-q5_0.bin")
            full = os.path.join(models, "ggml-large-v3-turbo.bin")
            open(full, "wb").close()
            open(q5, "wb").close()

            self.assertEqual(
                voice.resolve_whisper_model(explicit="", module_dir=tmp, cwd="/tmp/nowhere"),
                q5,
            )

    def test_finds_repo_models_from_installed_module_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = os.path.join(tmp, "Application Support", "StickS3TaskHub")
            repo_models = os.path.join(tmp, "host", "models")
            os.makedirs(app_dir)
            os.makedirs(repo_models)
            q5 = os.path.join(repo_models, "ggml-large-v3-turbo-q5_0.bin")
            open(q5, "wb").close()

            self.assertEqual(
                voice.resolve_whisper_model(explicit="", module_dir=app_dir, cwd=tmp),
                q5,
            )


if __name__ == "__main__":
    unittest.main()
