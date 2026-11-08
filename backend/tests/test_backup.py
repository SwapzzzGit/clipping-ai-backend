import zipfile

from app.backup import write_backup


def names_in(archive) -> set[str]:
    with zipfile.ZipFile(archive) as z:
        return set(z.namelist())


def test_backup_leaves_out_bulky_folders_and_keeps_the_rest(tmp_path):
    data = tmp_path / "data"
    for rel in ("jobs/a.json", "publish/accounts.json", "secret.key", "uploads/v.mp4", "output/1/c.mp4", "work/x.wav"):
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        (data / rel).write_bytes(b"x")

    assert names_in(write_backup(data, tmp_path / "out")) == {"jobs/a.json", "publish/accounts.json", "secret.key"}
    assert "uploads/v.mp4" in names_in(write_backup(data, tmp_path / "out", include_all=True))
