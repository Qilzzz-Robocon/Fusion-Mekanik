# HollowSurfaceAddIn - Autodesk Fusion add-in (Python)
#
# Command: Hollow Surface (Solid > Modify)
#   1. Pick the surface(s) to OPEN, i.e. the faces that are removed. Bodies are not selected.
#   2. "Tembus ujung ke ujung" (default on): the opposite end face of the same body is opened too,
#      so the cavity runs through from end to end (a tube / sleeve).
#   3. Wall thickness is a value input in mm. Changing it updates the hollow live, like the Flange tool.
#      The shell thickness is edited in place; if Fusion does not allow that, the shell is rebuilt.
#
# OK keeps the feature. Cancel removes it again, so the body goes back to what it was.
# Last thickness and the "through" option are saved in hollow_surface_settings.json.

import adsk.core
import adsk.fusion
import json
import os
import time
import traceback

ADDIN_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(ADDIN_DIR, 'hollow_surface_settings.json')
LOG_PATH = os.path.join(ADDIN_DIR, 'hollow_surface.log')
CMD_ID = 'HollowSurface_Cmd'
PANEL_ID = 'SolidModifyPanel'
DEFAULTS = {'thickness_mm': 2.0, 'through': True}

app = None
ui = None
handlers = []     # keep references so Fusion does not garbage-collect the event handlers
session = None    # state of the running Hollow Surface command (None when not running)


# ------------------------------------------------------------------ settings and log
def log(text):
    try:
        with open(LOG_PATH, 'a', encoding='utf-8') as f:
            f.write('%s  %s\n' % (time.strftime('%Y-%m-%d %H:%M:%S'), text))
    except Exception:
        pass


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, 'r', encoding='utf-8') as f:
            s.update(json.load(f))
    except Exception:
        pass   # missing or broken file -> defaults
    return s


def save_settings(s):
    with open(SETTINGS_PATH, 'w', encoding='utf-8') as f:
        json.dump(s, f, indent=2)


# ------------------------------------------------------------------ geometry helpers
def opposite_face(face):
    """The far end face of the same body: parallel, facing the opposite way, same size.
    Returns None if there is none (e.g. the body is not a prism along this face's normal)."""
    p = face.pointOnFace
    ok, n = face.evaluator.getNormalAtPoint(p)
    if not ok:
        return None
    best, best_dist = None, 0.0
    for other in face.body.faces:
        if other.entityToken == face.entityToken:
            continue
        if other.geometry.objectType != adsk.core.Plane.classType():
            continue
        if abs(other.area - face.area) > 0.01 * face.area:
            continue
        ok2, m = other.evaluator.getNormalAtPoint(other.pointOnFace)
        if not ok2 or m.dotProduct(n) > -0.999:
            continue
        # the body lies on the -n side of an outward face, so the far end has a negative distance
        dist = n.dotProduct(p.vectorTo(other.pointOnFace))
        if dist < -1e-6 and (best is None or dist < best_dist):
            best, best_dist = other, dist
    return best


def tokens_for(faces, through):
    """Entity tokens of the faces to open (picked faces, plus their opposite ends if `through`)."""
    tokens = []
    for f in faces:
        if f.entityToken not in tokens:
            tokens.append(f.entityToken)
        if through:
            other = opposite_face(f)
            if other is not None and other.entityToken not in tokens:
                tokens.append(other.entityToken)
    return tokens


def resolve(design, token):
    found = design.findEntityByToken(token)
    if not found:
        raise RuntimeError('surface tidak ditemukan lagi (mungkin sudah berubah)')
    return found[0]


# ------------------------------------------------------------------ feature handling
def create_shell(thickness_mm):
    design = session['design']
    faces = [resolve(design, t) for t in session['tokens']]
    comp = faces[0].body.parentComponent
    coll = adsk.core.ObjectCollection.create()
    for f in faces:
        coll.add(f)
    shells = comp.features.shellFeatures
    sin = shells.createInput(coll, False)
    sin.insideThickness = adsk.core.ValueInput.createByReal(thickness_mm / 10.0)   # Fusion units are cm
    return shells.add(sin)


def remove_feature():
    feat = session['feature']
    session['feature'] = None
    if feat is not None:
        try:
            feat.deleteMe()
        except Exception as e:   # Fusion may already have dropped the preview feature on cancel
            log('remove_feature: %s' % str(e).splitlines()[0])


def apply(thickness_mm):
    """Create the hollow the first time; afterwards change its thickness in place so the model updates live."""
    feat = session['feature']
    if feat is not None:
        try:
            feat.insideThickness.value = thickness_mm / 10.0
            return
        except Exception:
            log('in-place thickness change not possible, rebuilding: %s' % traceback.format_exc().splitlines()[-1])
            remove_feature()
    session['feature'] = create_shell(thickness_mm)


def sync(rebuild):
    """rebuild=True when the surface selection or the 'through' option changed.
    Returns False if no surface is selected yet."""
    inputs = session['inputs']
    if rebuild:
        remove_feature()
        session['tokens'] = None
    if session['tokens'] is None:
        sel = inputs.itemById('faces')
        faces = [sel.selection(i).entity for i in range(sel.selectionCount)]
        if not faces:
            return False
        session['tokens'] = tokens_for(faces, bool(inputs.itemById('through').value))
    apply(inputs.itemById('thickness').value * 10.0)   # ValueInput comes back in cm
    return True


# ------------------------------------------------------------------ command handlers
class CreatedHandler(adsk.core.CommandCreatedEventHandler):
    def notify(self, args):
        global session
        try:
            design = adsk.fusion.Design.cast(app.activeProduct)
            if design is None:
                ui.messageBox('Buka dokumen desain (bukan drawing) dulu.')
                return
            cmd = args.command
            s = load_settings()
            inputs = cmd.commandInputs
            sel = inputs.addSelectionInput('faces', 'Surface yang dibuka',
                                           'Pilih surface yang akan dibuka (boleh dua ujung sekaligus)')
            sel.addSelectionFilter('Faces')
            # 0 = optional: the shell removes the picked face, so Fusion clears the selection right after the
            # preview. A required selection would then grey out OK. The pick is kept in session['tokens'] instead.
            sel.setSelectionLimits(0, 0)
            inputs.addBoolValueInput('through', 'Tembus ujung ke ujung', True, '', bool(s['through']))
            inputs.addValueInput('thickness', 'Ketebalan dinding', 'mm',
                                 adsk.core.ValueInput.createByString('%g mm' % s['thickness_mm']))
            session = {'design': design, 'inputs': inputs, 'tokens': None, 'feature': None, 'executed': False}
            log('Hollow Surface started')
            for event, handler_cls in ((cmd.inputChanged, InputChangedHandler),
                                       (cmd.executePreview, PreviewHandler),
                                       (cmd.execute, ExecuteHandler),
                                       (cmd.destroy, DestroyHandler)):
                h = handler_cls()
                event.add(h)
                handlers.append(h)
        except Exception:
            ui.messageBox('Hollow Surface error:\n%s' % traceback.format_exc())


class InputChangedHandler(adsk.core.InputChangedEventHandler):
    def notify(self, args):
        try:
            if session is None:
                return
            if args.input.id == 'faces' and session['feature'] is not None and \
                    session['inputs'].itemById('faces').selectionCount == 0:
                log('selection cleared by the shell itself, keeping the hollow')
                return
            if args.input.id in ('faces', 'through'):
                log('%s changed: %d surface(s) picked' % (args.input.id, session['inputs'].itemById('faces').selectionCount))
                if not sync(True):
                    log('no surface picked yet, OK stays disabled')
            elif args.input.id == 'thickness':
                sync(False)
        except Exception:
            log(traceback.format_exc())


class PreviewHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            log('preview requested')
            ok = session is not None and sync(False)
            log('preview done: picked=%s, feature=%s' % (ok, session['feature'] is not None if session else None))
        except Exception:
            log(traceback.format_exc())
        finally:
            # Never let the preview grey out OK. Execute checks the picks and tells the user if something is missing.
            args.isValidResult = True


class ExecuteHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        try:
            log('OK pressed')
            if session is None or not sync(False):
                ui.messageBox('Belum ada surface yang dipilih. Jalankan Hollow Surface lagi dan pilih surface dulu.')
                return
            inputs = session['inputs']
            save_settings({'thickness_mm': round(inputs.itemById('thickness').value * 10.0, 3),
                           'through': bool(inputs.itemById('through').value)})
            session['executed'] = True
            log('hollow surface kept: %d face(s), thickness %s' % (len(session['tokens']),
                                                                  inputs.itemById('thickness').value * 10.0))
        except Exception:
            ui.messageBox('Hollow Surface error:\n%s' % traceback.format_exc())


class DestroyHandler(adsk.core.CommandEventHandler):
    def notify(self, args):
        global session
        try:
            reason = args.terminationReason
            cancelled = reason in (adsk.core.CommandTerminationReason.CancelledTerminationReason,
                                   adsk.core.CommandTerminationReason.AbortedTerminationReason)
            if cancelled:
                remove_feature()          # Cancel: take the hollow back out
                log('hollow surface cancelled, feature removed')
            elif session is not None and session['feature'] is not None:
                # OK. Fusion can finish with the preview result without firing execute, so the hollow is kept
                # here and the settings are saved here too.
                inputs = session['inputs']
                save_settings({'thickness_mm': round(inputs.itemById('thickness').value * 10.0, 3),
                               'through': bool(inputs.itemById('through').value)})
                log('hollow surface kept (reason %s)' % reason)
        except Exception:
            log(traceback.format_exc())
        finally:
            session = None


# ------------------------------------------------------------------ add-in entry points
def run(context):
    global app, ui
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        if not os.path.exists(SETTINGS_PATH):
            save_settings(dict(DEFAULTS))
        panel = ui.allToolbarPanels.itemById(PANEL_ID)
        if panel is None:
            raise RuntimeError('panel %s tidak ditemukan (buka workspace Design)' % PANEL_ID)
        old_ctrl = panel.controls.itemById(CMD_ID)
        if old_ctrl is not None:
            old_ctrl.deleteMe()
        old_def = ui.commandDefinitions.itemById(CMD_ID)
        if old_def is not None:
            old_def.deleteMe()
        cdef = ui.commandDefinitions.addButtonDefinition(
            CMD_ID, 'Hollow Surface', 'Hollow dari surface yang dipilih, tebal dinding bisa diatur langsung', '')
        h = CreatedHandler()
        cdef.commandCreated.add(h)
        handlers.append(h)
        panel.controls.addCommand(cdef)
        log('add-in loaded')
    except Exception:
        msg = 'Hollow Surface add-in gagal dimuat:\n%s' % traceback.format_exc()
        if ui is not None:
            ui.messageBox(msg)
        else:
            log(msg)


def stop(context):
    global session
    try:
        if session is not None:
            remove_feature()
            session = None
        if ui is not None:
            panel = ui.allToolbarPanels.itemById(PANEL_ID)
            if panel is not None:
                ctrl = panel.controls.itemById(CMD_ID)
                if ctrl is not None:
                    ctrl.deleteMe()
            cdef = ui.commandDefinitions.itemById(CMD_ID)
            if cdef is not None:
                cdef.deleteMe()
        log('add-in stopped')
    except Exception:
        log(traceback.format_exc())
