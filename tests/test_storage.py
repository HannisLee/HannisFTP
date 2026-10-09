
from app.models import ProfileCreate, ProfileUpdate
from app.storage import Storage


async def test_profiles_and_transfer_persistence(tmp_path):
    storage = Storage(tmp_path / "test.sqlite3")
    await storage.connect()
    try:
        profile = await storage.create_profile(ProfileCreate(name="Test", host="example.com"))
        assert (await storage.list_profiles())[0].id == profile.id
        updated = await storage.update_profile(profile.id, ProfileUpdate(username="alice"))
        assert updated.username == "alice"
        await storage.delete_profile(profile.id)
        assert await storage.list_profiles() == []
    finally:
        await storage.close()


async def test_startup_marks_active_tasks_interrupted(tmp_path):
    storage = Storage(tmp_path / "test.sqlite3")
    await storage.connect()
    try:
        from app.models import TransferOut, utc_now
        task = TransferOut(
            task_id="t", profile_id="p", direction="upload", source_path="/a", destination_path="/b",
            total_bytes=10, transferred_bytes=2, status="running", created_at=utc_now(),
            conflict_strategy="overwrite",
        )
        await storage.save_transfer(task)
        await storage.mark_startup_interrupted()
        restored = await storage.get_transfer("t")
        assert restored.status == "interrupted"
        assert "restarted" in restored.error_message
    finally:
        await storage.close()
