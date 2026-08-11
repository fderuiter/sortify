import os
import wave
from unittest.mock import MagicMock, patch

from app.core.extractor_strategies import AudioExtractor


def create_compliant_wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)  # 16-bit PCM
        w.setframerate(16000)  # 16kHz
        w.writeframes(b"\x00" * 32000)  # 1 second of silence


def create_non_compliant_wav(path):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)  # 8-bit PCM (non-compliant)
        w.setframerate(8000)  # 8kHz (non-compliant)
        w.writeframes(b"\x00" * 8000)


def test_compliant_wav_bypasses_transcoding(tmp_path):
    compliant_wav = tmp_path / "compliant.wav"
    create_compliant_wav(compliant_wav)

    extractor = AudioExtractor()

    # Mock spawn_background_process so we don't actually run Whisper
    mock_process = MagicMock()
    mock_process.stdout.readline.side_effect = ["Hello segment 1\n", ""]
    mock_process.returncode = 0

    with (
        patch(
            "app.core.env_helper.spawn_background_process", return_value=mock_process
        ) as mock_spawn,
        patch("shutil.which") as mock_which,
    ):
        text = extractor.extract(str(compliant_wav))

        # Verification:
        # Since the WAV was compliant, shutil.which("ffmpeg") should NOT be called to find ffmpeg,
        # or at least no transcoding should occur.
        assert not mock_which.called

        # Whisper should be spawned with the original file path
        mock_spawn.assert_called_once()
        cmd_arg = mock_spawn.call_args[0][0]
        assert str(compliant_wav) in cmd_arg

        assert "Hello segment 1" in text


def test_non_compliant_mp3_transcoded_and_cleaned_up(tmp_path):
    mp3_file = tmp_path / "test.mp3"
    with open(mp3_file, "wb") as f:
        f.write(b"fake mp3 audio content")

    extractor = AudioExtractor()

    mock_whisper = MagicMock()
    mock_whisper.stdout.readline.side_effect = ["Hello segment 1\n", ""]
    mock_whisper.returncode = 0

    ffmpeg_output_path = []

    def mock_run_background_process(cmd, **kwargs):
        # We expect: ['ffmpeg', '-y', '-i', str(mp3_file), '-acodec', 'pcm_s16le', '-ar', '16000', <temp_path>]
        assert cmd[0] == "ffmpeg"
        assert cmd[1] == "-y"
        assert cmd[2] == "-i"
        assert cmd[3] == str(mp3_file)
        assert cmd[4] == "-acodec"
        assert cmd[5] == "pcm_s16le"
        assert cmd[6] == "-ar"
        assert cmd[7] == "16000"

        out_path = cmd[8]
        ffmpeg_output_path.append(out_path)
        # Create a compliant WAV at the output path so get_audio_duration and wave reading succeeds
        create_compliant_wav(out_path)

        res = MagicMock()
        res.returncode = 0
        return res

    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "app.core.env_helper.run_background_process",
            side_effect=mock_run_background_process,
        ) as mock_run,
        patch(
            "app.core.env_helper.spawn_background_process", return_value=mock_whisper
        ) as mock_spawn,
    ):
        text = extractor.extract(str(mp3_file))

        # Verification:
        # ffmpeg should have been run once
        mock_run.assert_called_once()
        assert len(ffmpeg_output_path) == 1
        temp_wav = ffmpeg_output_path[0]

        # Whisper should be run with the temporary WAV file and NOT the original MP3 file
        mock_spawn.assert_called_once()
        cmd_arg = mock_spawn.call_args[0][0]
        assert temp_wav in cmd_arg
        assert str(mp3_file) not in cmd_arg

        # The transcript is returned correctly
        assert "Hello segment 1" in text

        # The temporary WAV file MUST be cleaned up and deleted immediately
        assert not os.path.exists(temp_wav)


def test_non_compliant_wav_transcoded_and_cleaned_up(tmp_path):
    non_compliant_wav = tmp_path / "non_compliant.wav"
    create_non_compliant_wav(non_compliant_wav)

    extractor = AudioExtractor()

    mock_whisper = MagicMock()
    mock_whisper.stdout.readline.side_effect = ["Hello segment 1\n", ""]
    mock_whisper.returncode = 0

    ffmpeg_output_path = []

    def mock_run_background_process(cmd, **kwargs):
        out_path = cmd[8]
        ffmpeg_output_path.append(out_path)
        create_compliant_wav(out_path)
        res = MagicMock()
        res.returncode = 0
        return res

    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch(
            "app.core.env_helper.run_background_process",
            side_effect=mock_run_background_process,
        ) as mock_run,
        patch(
            "app.core.env_helper.spawn_background_process", return_value=mock_whisper
        ) as mock_spawn,
    ):
        text = extractor.extract(str(non_compliant_wav))

        # Transcoding should have occurred
        mock_run.assert_called_once()
        assert len(ffmpeg_output_path) == 1
        temp_wav = ffmpeg_output_path[0]

        # Whisper should run on the transcoded WAV file
        mock_spawn.assert_called_once()
        cmd_arg = mock_spawn.call_args[0][0]
        assert temp_wav in cmd_arg

        assert "Hello segment 1" in text

        # Temporary file is cleaned up
        assert not os.path.exists(temp_wav)


def test_ffmpeg_missing_returns_error(tmp_path):
    mp3_file = tmp_path / "test.mp3"
    with open(mp3_file, "wb") as f:
        f.write(b"fake mp3 audio content")

    extractor = AudioExtractor()

    with (
        patch("shutil.which", return_value=None) as mock_which,
        patch("app.core.env_helper.run_background_process") as mock_run,
    ):
        text = extractor.extract(str(mp3_file))

        mock_which.assert_called_once_with("ffmpeg")
        # Transcoding and transcription should not be run
        assert not mock_run.called
        assert "FFmpeg binary not found" in text


def test_transcoding_failure_returns_error(tmp_path):
    mp3_file = tmp_path / "test.mp3"
    with open(mp3_file, "wb") as f:
        f.write(b"fake mp3 audio content")

    extractor = AudioExtractor()

    mock_result = MagicMock()
    mock_result.returncode = 1
    mock_result.stderr = "Error decoding audio stream"

    with (
        patch("shutil.which", return_value="/usr/bin/ffmpeg"),
        patch("app.core.env_helper.run_background_process", return_value=mock_result),
        patch("app.core.env_helper.spawn_background_process") as mock_spawn,
    ):
        text = extractor.extract(str(mp3_file))

        # Transcription should not be run
        assert not mock_spawn.called
        assert "FFmpeg transcoding failed" in text
