import importlib.util
import subprocess


def scanner(root):
    spec=importlib.util.spec_from_file_location("project_checks",root/"scripts/check_project.py")
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.scan


def test_scan_detects_literal_canary_and_omits_its_value(root,tmp_path):
    subprocess.run(["git","init","-q",str(tmp_path)],check=True,capture_output=True)
    key="WEREAD"+"_API_KEY"
    canary="canary"+"-not-real-742119"
    (tmp_path/"bad.py").write_text(key+" = '"+canary+"'\n",encoding="utf-8")
    result=scanner(root)(tmp_path)
    assert result["status"]=="FAIL"
    assert len(result["findings"])==1
    assert canary not in str(result)


def test_scan_distinguishes_source_expressions_from_credential_literals(root,tmp_path):
    subprocess.run(["git","init","-q",str(tmp_path)],check=True,capture_output=True)
    (tmp_path/"safe.py").write_text('wr_skey=value["session"]\nheaders={"Authorization":"Bearer " + key}\n',encoding="utf-8")
    assert scanner(root)(tmp_path)["status"]=="PASS"
