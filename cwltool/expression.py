import docker
import subprocess
import json
from aslist import aslist
import logging
import os
from errors import WorkflowException
import process
import yaml
import schema_salad.validate as validate
import schema_salad.ref_resolver
import sandboxjs
import re

_logger = logging.getLogger("cwltool")

def jshead(engineConfig, jobinput, context, tmpdir, outdir):
    return """
%s
var inputs=%s;
var self=%s;
var runtime={'tmpdir': %s, 'outdir': %s};
""" % ("\n".join(engineConfig),
                      json.dumps(jobinput, indent=4),
                      json.dumps(context, indent=4),
                      json.dumps(tmpdir, indent=4),
                      json.dumps(outdir, indent=4))

def exeval(ex, jobinput, requirements, outdir, tmpdir, context, pull_image):
    if ex["engine"] == "https://w3id.org/cwl/cwl#JsonPointer":
        try:
            obj = {"job": jobinput, "context": context, "outdir": outdir, "tmpdir": tmpdir}
            return schema_salad.ref_resolver.resolve_json_pointer(obj, ex["script"])
        except ValueError as v:
            raise WorkflowException("%s in %s" % (v,  obj))

    if ex["engine"] == "https://w3id.org/cwl/cwl#JavascriptEngine":
        engineConfig = []
        for r in reversed(requirements):
            if r["class"] == "ExpressionEngineRequirement" and r["id"] == "https://w3id.org/cwl/cwl#JavascriptEngine":
                engineConfig = r.get("engineConfig", [])
                break
        return sandboxjs.execjs(ex["script"], jshead(engineConfig, jobinput, context, tmpdir, outdir))

    for r in reversed(requirements):
        if r["class"] == "ExpressionEngineRequirement" and r["id"] == ex["engine"]:
            runtime = []

            class DR(object):
                pass
            dr = DR()
            dr.requirements = r.get("requirements", [])
            dr.hints = r.get("hints", [])

            (docker_req, docker_is_req) = process.get_feature(dr, "DockerRequirement")
            img_id = None
            if docker_req:
                img_id = docker.get_from_requirements(docker_req, docker_is_req, pull_image)
            if img_id:
                runtime = ["docker", "run", "-i", "--rm", img_id]

            inp = {
                "script": ex["script"],
                "engineConfig": r.get("engineConfig", []),
                "job": jobinput,
                "context": context,
                "outdir": outdir,
                "tmpdir": tmpdir,
            }

            _logger.debug("Invoking expression engine %s with %s",
                          runtime + aslist(r["engineCommand"]),
                                           json.dumps(inp, indent=4))

            sp = subprocess.Popen(runtime + aslist(r["engineCommand"]),
                             shell=False,
                             close_fds=True,
                             stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE)

            (stdoutdata, stderrdata) = sp.communicate(json.dumps(inp) + "\n\n")
            if sp.returncode != 0:
                raise WorkflowException("Expression engine returned non-zero exit code on evaluation of\n%s" % json.dumps(inp, indent=4))

            return json.loads(stdoutdata)

    raise WorkflowException("Unknown expression engine '%s'" % ex["engine"])

seg_symbol = r"""[^[\].(){} ]+"""
seg_single = r"""\['([^']|\\')+'\]"""
seg_double = r"""\["([^"]|\\")+"\]"""
seg_index  = r"""\[[0-9]+\]"""
segments = r"(\.%s|%s|%s|%s)" % (seg_symbol, seg_single, seg_double, seg_index)
segment_re = re.compile(segments)
param_re = re.compile(r"\$\((%s)%s*\)" % (seg_symbol, segments))

def next_seg(remain, obj):
    if remain:
        print remain
        m = segment_re.match(remain)
        if m.group(0)[0] == '.':
            return next_seg(remain[m.end(0):], obj[m.group(0)[1:]])
        else:
            key = m.group(0)[2:-2].replace("\\'", "'").replace('\\"', '"')
            return next_seg(remain[m.end(0):], obj[key])
    else:
        return obj

def param_interpolate(ex, obj, strip=True):
    m = param_re.search(ex)
    if m:
        print "=", m.group(0), "/", m.group(0)[m.end(1) - m.start(0):-1]
        leaf = next_seg(m.group(0)[m.end(1) - m.start(0):-1], obj[m.group(1)])
        if strip and len(ex.strip()) == len(m.group(0)):
            return leaf
        else:
            leaf = json.dumps(leaf)
            if leaf[0] == '"':
                leaf = leaf[1:-1]
            return ex[0:m.start(0)] + leaf + param_interpolate(ex[m.end(0):], obj, False)
    else:
        return ex


def do_eval(ex, jobinput, requirements, outdir, tmpdir, context=None, pull_image=True):
    if isinstance(ex, dict) and "engine" in ex and "script" in ex:
        return exeval(ex, jobinput, requirements, outdir, tmpdir, context, pull_image)
    if isinstance(ex, basestring):
        for r in requirements:
            if r["class"] == "InlineJavascriptRequirement":
                return sandboxjs.interpolate(ex, jshead(r.get("expressionLib", []), jobinput, context, tmpdir, outdir))
        return param_interpolate(ex, {
            "inputs": jobinput,
            "self": context,
            "runtime": {
                "tmpdir": tmpdir,
                "outdir": outdir
            }
        })
    return ex
