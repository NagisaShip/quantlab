def test_create_app_tolerates_missing_frontend_build(tmp_path, monkeypatch):
    """前端构建目录可能正在被 npm/Vite 清空重建；API 启动不应因此报错。"""
    from quant_agent.api import app as appmod

    monkeypatch.setattr(appmod, "WEB_DIST", tmp_path / "missing-dist")
    app = appmod.create_app()
    assert app.title
