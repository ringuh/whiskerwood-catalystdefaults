"""Generates the Blueprint paste text (T3D) for CatalystDefaults into tools/out/.
Usage: python tools/catalystdefaults_build.py   (needs the modkit's jmap, see t3d.py)
Casts are impure (pure casts paste as impure in 5.6 and get pruned).
Paste each file into the matching asset's event graph (Ctrl+A, Delete, Ctrl+V), compile.

How the mod works (game 0.7.207):
- A production building's Catalyst switch is Industry.m_allowCatalysts (off for a new building).
  IndustryDetails' native ReceiveHudAction handles "setCatalystUse" (paramBool) for the building
  that is the view's Context, the same command as the window's switch. Context is not
  Blueprint-writable, so it is set with SetObjectPropertyByName on one invisible IndustryDetails.
- BP_MapLoad, event-driven only (no tick, no looping timer, no scan of all buildings):
  * onConstructionSpawned: binds that site's OnDestroyed (fires when it finishes or is cancelled).
  * OnSiteGone: reads the site's m_gridActorToBuild (row name in data table GridactorDefs_Sync).
    Buildings whose row has neither asIndustryDef nor catalyst (houses, walls, belts, ...) stop
    here. For production buildings the row's GridActor column gives the class; the site's root
    cell + location go into a small pending list and a one-shot 1 s check timer starts (if not
    already running).
  * OnCheck: for each pending entry, lists only actors of that one class and picks the one
    standing on the site's root cell (or exactly at its location). That building gets its
    catalyst turned on. Entries not found are retried up to 4 times (cancelled sites end here).
  * onBuildingSpawned (buildings placed without a construction site): handles exactly that actor.
  Buildings that exist when a save loads are never looked at.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from t3d import *

OUT = os.path.join(os.path.dirname(__file__), 'out')
os.makedirs(OUT, exist_ok=True)
M = '/Game/Mods/CatalystDefaults/'
MODNAME = M.rstrip('/').split('/')[-1]   # = installed folder Saved\\mods\\<MODNAME>
STARTUP, MAPLOAD = M + 'BP_Startup', M + 'BP_MapLoad'
KSL = '/Script/Engine.KismetSystemLibrary'
KML = '/Script/Engine.KismetMathLibrary'
KSTR = '/Script/Engine.KismetStringLibrary'
KAL = '/Script/Engine.KismetArrayLibrary'
API = '/Script/SystemCore.ModAPI'
PA = '/Script/ProjectArco.'
GA, AWB = PA + 'GridActor', PA + 'ArcoWidgetBase'
INDC, INDV, SITE = PA + 'Industry', PA + 'IndustryDetails', PA + 'ConstructionSite'
OPT_ID = 'CatalystDefaults_On'
TABLES = ['GridDefsSync', 'GridActors']   # ModAPI names (from ListDataTables); first one found is used
TRIES = '4'

ACTOR = OBJ('/Script/Engine.Actor')
ACLS = CLS('/Script/Engine.Actor')
VEC = STRUCT('/Script/CoreUObject.Vector')
IV_T = OBJ(INDV)


def modapi(g, x, y, name='Api'):
    return g.call(API + ':GetModAPI', name, x, y)


def api_call(g, fn, x, y, name=None, **kw):
    a = modapi(g, x - 250, y + 150, name=(name or fn) + 'Api')
    n = g.call(API + ':' + fn, name or fn, x, y, **kw)
    link(a['ReturnValue'], n['self'])
    return n


class _Gate(Node):
    """Exec block 'if Debug: LogMessage', joined again by a reroute knot. Not a graph node itself."""
    def __init__(self, entry, exit_):
        self._e, self._x = entry, exit_; self.name = entry.node.name
    def __getitem__(self, k):
        return {'execute': self._e, 'then': self._x}[k]


def log(g, x, y, msg_pin=None, msg=None, name='Log'):
    """Debug-only log line: runs only when the Debug variable is true (debug.txt next to the pak)."""
    dg = g.get('Debug', BOOL, x - 200, y + 120, name=name + 'DebugGet')
    br = g.branch(x - 150, y, name + 'IfDebug'); link(dg['Debug'], br['Condition'])
    n = api_call(g, 'LogMessage', x, y, name=name, doPrependDate='true')
    if msg_pin: link(msg_pin, n['Msg'])
    elif msg: n.set('Msg', msg)
    ex(br, n)
    k = g.add(BG + 'K2Node_Knot', name + 'Join', [], x + 250, y - 40)
    k.pin('InputPin', EXEC); k.pin('OutputPin', EXEC, out=True)
    link(n['then'], k['InputPin']); link(br['else'], k['InputPin'])
    return _Gate(br['execute'], k['OutputPin'])


def concat(g, x, y, *parts):
    cur = None
    for i, p in enumerate(parts):
        if cur is None:
            if isinstance(p, str):
                c = g.call(KSTR + ':Concat_StrStr', 'Cat', x, y); c.set('A', p); cur = c['ReturnValue']; continue
            cur = p; continue
        c = g.call(KSTR + ':Concat_StrStr', 'Cat', x + 40 * i, y + 30 * i)
        link(cur, c['A'])
        if isinstance(p, str): c.set('B', p)
        else: link(p, c['B'])
        cur = c['ReturnValue']
    return cur


def is_valid(g, pin, x, y, name='Valid'):
    n = g.call(KSL + ':IsValid', name, x, y); link(pin, n['Object']); return n['ReturnValue']


def struct_node(g, kind, spath, show, x, y, name):
    """kind 'Make' or 'Break'; show = list of property names to expose."""
    props = J()[spath]['properties']
    n = g.add(BG + 'K2Node_%sStruct' % kind, name,
              ['StructType="/Script/CoreUObject.ScriptStruct\'%s\'"' % spath, 'bMadeAfterOverridePinRemoval=True'], x, y)
    for i, p in enumerate(props):
        n.header.append('ShowPinForProperties(%d)=(PropertyName="%s",bShowPin=%s,bCanToggleVisibility=True)' % (i, p['name'], p['name'] in show))
    sname = spath.split('.')[-1]
    if kind == 'Break':
        n.pin(sname, STRUCT(spath))
    for p in props:
        if p['name'] in show:
            d = None
            if kind == 'Make':
                d = {'IntProperty': '0', 'StrProperty': '', 'BoolProperty': 'false'}.get(p['type'])
            n.pin(p['name'], prop_type(p), out=(kind == 'Break'), default=d)
    if kind == 'Make':
        n.pin(sname, STRUCT(spath), out=True)
    return n


def array_item(g, arr_pin, elem_t, idx_pin, x, y, name):
    """K2Node_GetArrayItem (the array [] Get node), by copy."""
    n = g.add(BG + 'K2Node_GetArrayItem', name, ['bReturnByRefDesired=False'], x, y)
    at = ARR(elem_t); at['ref'] = True; at['const'] = True
    n.pin('Array', at); n.pin('Dimension 1', INT, default='0'); n.pin('Output', elem_t, out=True)
    link(arr_pin, n['Array']); link(idx_pin, n['Dimension 1'])
    return n['Output']


def arr_fn(g, fn, elem_t, x, y, name):
    """Impure KismetArrayLibrary call with explicit element type: Array_Add / Array_Set / Array_Clear."""
    n = g.add(BG + 'K2Node_CallArrayFunction', name,
              ['FunctionReference=(MemberParent="%s",MemberName="%s")' % (cls_ref(KAL), fn)], x, y)
    n.pin('execute', EXEC); n.pin('then', EXEC, out=True)
    n.pin('self', OBJ(KAL), defobj='/Script/Engine.Default__KismetArrayLibrary', hidden=True, friendly='NSLOCTEXT("K2Node", "Target", "Target")')
    at = ARR(elem_t); at['ref'] = True
    n.pin('TargetArray', at)
    it = dict(elem_t); it['ref'] = True; it['const'] = True
    if fn == 'Array_Add':
        n.pin('NewItem', it); n.pin('ReturnValue', INT, out=True)
    elif fn == 'Array_Set':
        n.pin('Index', INT, default='0'); n.pin('Item', it); n.pin('bSizeToFit', BOOL, default='false')
    return n


def var_arr(g, var, elem_t, x, y, name):
    return g.get(var, ARR(elem_t), x, y, name=name)[var]


_orig_fmt = Pin.fmt
def _fmt(self):
    s = _orig_fmt(self)
    mr = getattr(self, 'memref', None)
    if mr: s = s.replace('PinType.PinSubCategoryMemberReference=()', 'PinType.PinSubCategoryMemberReference=(%s)' % mr, 1)
    return s
Pin.fmt = _fmt


def bind(g, target_pin, owner, delegate, sig_pkg, sig, event_node, x, y, name):
    n = g.add(BG + 'K2Node_AddDelegate', name,
              ['DelegateReference=(MemberParent="%s",MemberName="%s")' % (cls_ref(owner), delegate)], x, y)
    n.pin('execute', EXEC); n.pin('then', EXEC, out=True)
    n.pin('self', OBJ(owner), friendly='NSLOCTEXT("K2Node", "Target", "Target")')
    d = n.pin('Delegate', T('delegate'))
    d.memref = 'MemberParent="/Script/CoreUObject.Package\'%s\'",MemberName="%s"' % (sig_pkg, sig)
    link(target_pin, n['self'])
    ev = event_node['OutputDelegate']
    ev.memref = 'MemberParent="/Script/Engine.BlueprintGeneratedClass\'%s.%s_C\'",MemberName="%s"' % (MAPLOAD, MAPLOAD.split('/')[-1], event_node.name)
    link(ev, d)
    return n


def self_node(g, x, y, name):
    n = g.add(BG + 'K2Node_Self', name, [], x, y)
    n.pin('self', T('object', sub='self'), out=True)
    return n


def set_timer(g, x, y, name):
    n = g.call(KSL + ':K2_SetTimer', name, x, y, FunctionName='OnCheck', Time='1.0', bLooping='false')
    link(self_node(g, x - 200, y + 200, name + 'Self')['self'], n['Object'])
    return n


def class_cast(g, x, y, name):
    """Cast To Actor Class (K2Node_ClassDynamicCast), impure."""
    n = g.add(BG + 'K2Node_ClassDynamicCast', name, ['TargetType="%s"' % cls_ref('/Script/Engine.Actor')], x, y)
    n.pin('execute', EXEC); n.pin('then', EXEC, out=True); n.pin('CastFailed', EXEC, out=True)
    n.pin('Class', CLS('/Script/CoreUObject.Object'))
    n.pin('AsActor', ACLS, out=True)
    n.pin('bSuccess', BOOL, out=True, hidden=True)
    return n


def not_none(g, str_pin, x, y, name):
    """string is neither '' nor 'None'"""
    e = g.call(KSTR + ':IsEmpty', name + 'Empty', x, y); link(str_pin, e['InString'])
    ne = g.call(KML + ':Not_PreBool', name + 'NotEmpty', x + 200, y); link(e['ReturnValue'], ne['A'])
    nn = g.call(KSTR + ':NotEqual_StrStr', name + 'NotNone', x, y + 100, B='None'); link(str_pin, nn['A'])
    a = g.call(KML + ':BooleanAND', name, x + 400, y + 50); link(ne['ReturnValue'], a['A']); link(nn['ReturnValue'], a['B'])
    return a['ReturnValue']


# =========================================================================== BP_Startup
g = Graph(STARTUP)
bp = g.event('/Script/Engine.Actor', 'ReceiveBeginPlay', [], 'Begin', 0, 0)
vals = g.add(BG + 'K2Node_MakeArray', 'Vals', ['NumInputs=2'], 300, 250)
vals.pin('Array', ARR(STR), out=True)
vals.pin('[0]', STR, default='On'); vals.pin('[1]', STR, default='Off')
reg = api_call(g, 'RegisterModOptions', 600, 0, name='Reg',
               optionId=OPT_ID,
               optionDisplayName='Production buildings - Catalyst on when built',
               DefaultValue='On',
               optionDescription='When a production building with a Catalyst slot is finished, its Catalyst switch is turned on. '
                                 'Buildings that already exist are never changed. Game default is Off.')
link(vals['Array'], reg['Values'])
ex(bp, reg)
open(OUT + '/BP_Startup.txt', 'w', encoding='utf-8').write(g.text())

# =========================================================================== BP_MapLoad
# Variables: Debug (Boolean), View (IndustryDetails ref), Cur (Actor ref), TableName (Name), AnyLeft (Boolean),
#            PendClass (Actor Class Reference ARRAY), PendPos (String array), PendLoc (Vector array), PendTries (Integer array)
g = Graph(MAPLOAD)

# ---- BeginPlay: bind events, find the building table
bp = g.event('/Script/Engine.Actor', 'ReceiveBeginPlay', [], 'Begin', 0, -2400)
api = modapi(g, 200, -2150, name='BindApi')
evC = g.custom_event('OnConstruction', [('Actor', ACTOR)], 0, -1700)
evG = g.custom_event('OnSiteGone', [('DestroyedActor', ACTOR)], 0, -1000)
evB = g.custom_event('OnBuilt', [('Actor', ACTOR)], 0, 2600)
evT = g.custom_event('OnCheck', [], 0, 600)
b1 = bind(g, api['ReturnValue'], API, 'onConstructionSpawned', '/Script/SystemCore', 'ModAPI_OnActorSpawned__DelegateSignature', evC, 400, -2400, 'BindConstruction'); ex(bp, b1)
b2 = bind(g, api['ReturnValue'], API, 'onBuildingSpawned', '/Script/SystemCore', 'ModAPI_OnActorSpawned__DelegateSignature', evB, 700, -2400, 'BindBuilt'); ex(b1, b2)
evL = g.custom_event('OnLoaded', [], 0, -2900)
b0 = bind(g, api['ReturnValue'], API, 'onLoadingFinished', '/Script/SystemCore', 'ModAPI_OnEvent__DelegateSignature', evL, 1000, -2400, 'BindLoaded'); ex(b2, b0)
# table lookup after loading (data tables may not be registered yet at BeginPlay)
# Debug = a debug.txt (any content) exists in Saved\\mods\\<MODNAME>\\ next to the pak. Published builds ship without it.
rdf = api_call(g, 'ReadModTextFile', 200, -3200, name='ReadDebugFile', modName=MODNAME, Filename='debug.txt'); ex(evL, rdf)
dfe = g.call(KSTR + ':IsEmpty', 'DebugFileEmpty', 450, -3050); link(rdf['ReturnValue'], dfe['InString'])
dfn = g.call(KML + ':Not_PreBool', 'DebugFileThere', 650, -3050); link(dfe['ReturnValue'], dfn['A'])
sdb = g.setv('Debug', BOOL, 500, -3200, name='SetDebug'); link(dfn['ReturnValue'], sdb['Debug']); ex(rdf, sdb)
prev = (sdb, 'then')
for i, tname in enumerate(TABLES):
    h = api_call(g, 'HasDataTable', 300 + 600 * i, -2900, name='HasTable%d' % i, datatableName=tname); ex(prev[0], h, prev[1])
    bh = g.branch(550 + 600 * i, -2900, 'BrHasTable%d' % i); link(h['ReturnValue'], bh['Condition']); ex(h, bh)
    st = g.setv('TableName', NAME, 550 + 600 * i, -2700, value=tname, name='UseTable%d' % i); ex(bh, st)
    lr = log(g, 800 + 600 * i, -2700, msg='CatalystDefaults ready (table %s)' % tname, name='LogReady%d' % i); ex(st, lr)
    prev = (bh, 'else')
XL = 300 + 600 * len(TABLES)
lw = log(g, XL, -2900, msg='CatalystDefaults: building table not found - mod inactive. Tables the mod API knows:', name='LogNoTable')
ex(prev[0], lw, prev[1])
lt = api_call(g, 'ListDataTables', XL + 300, -2900, name='ListTables'); ex(lw, lt)
lpT = g.macro('ForEachLoop', NAME, XL + 600, -2900, name='TableLoop'); link(lt['ReturnValue'], lpT['Array']); ex(lt, lpT, 'then', 'Exec')
tns = g.call(KSTR + ':Conv_NameToString', 'TableNameStr', XL + 850, -2700); link(lpT['Array Element'], tns['InName'])
tmsg = concat(g, XL + 1050, -2700, 'CatalystDefaults:   table ', tns['ReturnValue'])
ltn = log(g, XL + 900, -2900, msg_pin=tmsg, name='LogTableName'); ex(lpT, ltn, 'LoopBody')

# ---- OnConstruction: watch this site (OnDestroyed = finished or cancelled)
b3 = bind(g, evC['Actor'], '/Script/Engine.Actor', 'OnDestroyed', '/Script/Engine', 'ActorDestroyedSignature__DelegateSignature', evG, 400, -1700, 'BindSiteGone')
ex(evC, b3)

# ---- OnSiteGone: production building? -> pending list
site = evG['DestroyedActor']
opt = api_call(g, 'ReadModOptionValue', 400, -1000, name='ReadOpt', optionId=OPT_ID, fallbackValue='On'); ex(evG, opt)
optOn = g.call(KSTR + ':EqualEqual_StrStr', 'OptOn', 650, -800, B='On'); link(opt['ReturnValue'], optOn['A'])
sc = g.call('/Script/Engine.Actor:GetComponentByClass', 'SiteComp', 400, -650, ComponentClass=SITE); link(site, sc['self'])
sc['ReturnValue'].t = OBJ(SITE)
scOk = is_valid(g, sc['ReturnValue'], 650, -650, 'SiteCompValid')
go = g.call(KML + ':BooleanAND', 'OptAndSite', 850, -750); link(optOn['ReturnValue'], go['A']); link(scOk, go['B'])
bgo = g.branch(800, -1000, 'BrWatch'); link(go['ReturnValue'], bgo['Condition']); ex(opt, bgo)
row = g.get('m_gridActorToBuild', NAME, 900, -550, owner=SITE, name='BuildName'); link(sc['ReturnValue'], row['self'])
tn = g.get('TableName', NAME, 900, -400, name='TableNameGet')
rInd = api_call(g, 'ReadDataTableValue', 1100, -1000, name='ReadIndustryDef', ColumnName='asIndustryDef')
rCat = api_call(g, 'ReadDataTableValue', 1400, -1000, name='ReadCatalyst', ColumnName='catalyst')
rCls = api_call(g, 'ReadDataTableValue', 2000, -1000, name='ReadClass', ColumnName='GridActor')
for r in (rInd, rCat, rCls):
    link(tn['TableName'], r['datatableName']); link(row['m_gridActorToBuild'], r['rowId'])
ex(bgo, rInd); ex(rInd, rCat)
prod = g.call(KML + ':BooleanOR', 'IsProduction', 1900, -650)
link(not_none(g, rInd['ReturnValue'], 1300, -700, 'HasIndustryDef'), prod['A'])
link(not_none(g, rCat['ReturnValue'], 1300, -500, 'HasCatalyst'), prod['B'])
bprod = g.branch(1700, -1000, 'BrProduction'); link(prod['ReturnValue'], bprod['Condition']); ex(rCat, bprod)
ex(bprod, rCls)
scp = g.call(KSL + ':MakeSoftClassPath', 'ClassPath', 2250, -800); link(rCls['ReturnValue'], scp['PathString'])
scr = g.call(KSL + ':Conv_SoftClassPathToSoftClassRef', 'ClassRef', 2450, -800); link(scp['ReturnValue'], scr['SoftClassPath'])
lca = g.call(KSL + ':LoadClassAsset_Blocking', 'LoadClass', 2300, -1000); link(scr['ReturnValue'], lca['AssetClass']); ex(rCls, lca)
cc = class_cast(g, 2600, -1000, 'AsActorClass'); link(lca['ReturnValue'], cc['Class']); ex(lca, cc)
# root cell + location of the site
sga = g.cast(GA, False, 2600, -500, name='SiteAsGridActor'); link(site, sga['Object'])
sga['AsGrid Actor'].name = 'AsGridActor'
sfp = g.get('liveFootprint', STRUCT(PA + 'GridFootprint'), 2850, -500, owner=GA, name='SiteFootprint'); link(sga['AsGridActor'], sfp['self'])
sfb = struct_node(g, 'Break', PA + 'GridFootprint', ['rootPosition'], 3100, -500, 'BreakSiteFootprint'); link(sfp['liveFootprint'], sfb['GridFootprint'])
spos = g.call(KSTR + ':Conv_IntVectorToString', 'SitePosStr', 3350, -500); link(sfb['rootPosition'], spos['InIntVec'])
sloc = g.call('/Script/Engine.Actor:K2_GetActorLocation', 'SiteLoc', 3350, -350); link(site, sloc['self'])
a1 = arr_fn(g, 'Array_Add', ACLS, 2900, -1000, 'PendAddClass'); link(var_arr(g, 'PendClass', ACLS, 2900, -1150, 'PendClassA'), a1['TargetArray']); link(cc['AsActor'], a1['NewItem']); ex(cc, sga); ex(sga, a1)
a2 = arr_fn(g, 'Array_Add', STR, 3150, -1000, 'PendAddPos'); link(var_arr(g, 'PendPos', STR, 3150, -1150, 'PendPosA'), a2['TargetArray']); link(spos['ReturnValue'], a2['NewItem']); ex(a1, a2)
a3 = arr_fn(g, 'Array_Add', VEC, 3400, -1000, 'PendAddLoc'); link(var_arr(g, 'PendLoc', VEC, 3400, -1150, 'PendLocA'), a3['TargetArray']); link(sloc['ReturnValue'], a3['NewItem']); ex(a2, a3)
a4 = arr_fn(g, 'Array_Add', INT, 3650, -1000, 'PendAddTries'); link(var_arr(g, 'PendTries', INT, 3650, -1150, 'PendTriesA'), a4['TargetArray']); a4.set('NewItem', TRIES); ex(a3, a4)
bnm = g.call(KSTR + ':Conv_NameToString', 'BuildNameStr', 3650, -650); link(row['m_gridActorToBuild'], bnm['InName'])
wmsg = concat(g, 3900, -650, 'CatalystDefaults: construction of ', bnm['ReturnValue'], ' ended at ', spos['ReturnValue'])
lwch = log(g, 3900, -1000, msg_pin=wmsg, name='LogWatch'); ex(a4, lwch)
ita = g.call(KSL + ':K2_IsTimerActive', 'CheckRunning', 4200, -800, FunctionName='OnCheck')
link(self_node(g, 4000, -750, 'CheckRunningSelf')['self'], ita['Object'])
bta = g.branch(4200, -1000, 'BrCheckRunning'); link(ita['ReturnValue'], bta['Condition']); ex(lwch, bta)
st1 = set_timer(g, 4450, -1000, 'StartCheck'); ex(bta, st1, 'else')
# class could not be resolved
cfm = concat(g, 2900, -1500, 'CatalystDefaults: no class for ', rCls['ReturnValue'])
lcf = log(g, 2900, -1300, msg_pin=cfm, name='LogNoClass'); ex(cc, lcf, 'CastFailed')

# ---- OnCheck: find each pending building among actors of its own class
sAny = g.setv('AnyLeft', BOOL, 250, 600, value='false', name='ResetAnyLeft'); ex(evT, sAny)
lpP = g.macro('ForEachLoop', INT, 500, 600, name='PendingLoop'); link(var_arr(g, 'PendTries', INT, 300, 800, 'PendTriesL'), lpP['Array']); ex(sAny, lpP, 'then', 'Exec')
idx = lpP['Array Index']; tries = lpP['Array Element']
tp = g.call(KML + ':Greater_IntInt', 'StillPending', 750, 850, B='0'); link(tries, tp['A'])
bp_ = g.branch(800, 600, 'BrPending'); link(tp['ReturnValue'], bp_['Condition']); ex(lpP, bp_, 'LoopBody')
dec = g.call(KML + ':Subtract_IntInt', 'TriesMinus1', 950, 850, B='1'); link(tries, dec['A'])
sdec = arr_fn(g, 'Array_Set', INT, 1050, 600, 'UseTry'); link(var_arr(g, 'PendTries', INT, 1050, 450, 'PendTriesS'), sdec['TargetArray'])
link(idx, sdec['Index']); link(dec['ReturnValue'], sdec['Item']); ex(bp_, sdec)
pcls = array_item(g, var_arr(g, 'PendClass', ACLS, 1100, 1000, 'PendClassG'), ACLS, idx, 1300, 1000, 'ThisClass')
ppos = array_item(g, var_arr(g, 'PendPos', STR, 1100, 1150, 'PendPosG'), STR, idx, 1300, 1150, 'ThisPos')
ploc = array_item(g, var_arr(g, 'PendLoc', VEC, 1100, 1300, 'PendLocG'), VEC, idx, 1300, 1300, 'ThisLoc')
gaa = g.call('/Script/Engine.GameplayStatics:GetAllActorsOfClass', 'ActorsOfThatClass', 1350, 600)
gaa['OutActors'].t = ARR(ACTOR)
link(pcls, gaa['ActorClass']); ex(sdec, gaa)
lpC = g.macro('ForEachLoop', ACTOR, 1650, 600, name='CandidateLoop'); link(gaa['OutActors'], lpC['Array']); ex(gaa, lpC, 'then', 'Exec')
cand = lpC['Array Element']
cga = g.cast(GA, False, 1700, 1000, name='CandAsGridActor'); link(cand, cga['Object'])
cga['AsGrid Actor'].name = 'AsGridActor'
cfp = g.get('liveFootprint', STRUCT(PA + 'GridFootprint'), 1950, 1000, owner=GA, name='CandFootprint'); link(cga['AsGridActor'], cfp['self'])
cfb = struct_node(g, 'Break', PA + 'GridFootprint', ['rootPosition'], 2200, 1000, 'BreakCandFootprint'); link(cfp['liveFootprint'], cfb['GridFootprint'])
cps = g.call(KSTR + ':Conv_IntVectorToString', 'CandPosStr', 2450, 1000); link(cfb['rootPosition'], cps['InIntVec'])
samePos = g.call(KSTR + ':EqualEqual_StrStr', 'SameCell', 2650, 1000); link(cps['ReturnValue'], samePos['A']); link(ppos, samePos['B'])
cloc = g.call('/Script/Engine.Actor:K2_GetActorLocation', 'CandLoc', 2200, 1250); link(cand, cloc['self'])
dist = g.call(KML + ':Vector_Distance', 'CandDist', 2450, 1250); link(cloc['ReturnValue'], dist['v1']); link(ploc, dist['v2'])
near = g.call(KML + ':Less_DoubleDouble', 'SameSpot', 2650, 1250, B='10.0'); link(dist['ReturnValue'], near['A'])
match = g.call(KML + ':BooleanOR', 'IsTheNewBuilding', 2850, 1100); link(samePos['ReturnValue'], match['A']); link(near['ReturnValue'], match['B'])
bm = g.branch(1950, 600, 'BrMatch'); link(match['ReturnValue'], bm['Condition']); ex(lpC, cga, 'LoopBody'); ex(cga, bm)
sdone = arr_fn(g, 'Array_Set', INT, 2200, 600, 'MarkFound'); link(var_arr(g, 'PendTries', INT, 2200, 450, 'PendTriesF'), sdone['TargetArray'])
link(idx, sdone['Index']); sdone.set('Item', '-1'); ex(bm, sdone)
curL = g.setv('Cur', ACTOR, 2450, 600, name='CurFromCheck'); link(cand, curL['Cur']); ex(sdone, curL)
# after the candidates: still pending? / given up?
left = array_item(g, var_arr(g, 'PendTries', INT, 1700, 300, 'PendTriesC'), INT, idx, 1900, 300, 'TriesLeftNow')
gl = g.call(KML + ':Greater_IntInt', 'TriesRemain', 2100, 300, B='0'); link(left, gl['A'])
bl = g.branch(2000, 100, 'BrTriesRemain'); link(gl['ReturnValue'], bl['Condition']); ex(lpC, bl, 'Completed')
sal = g.setv('AnyLeft', BOOL, 2300, 0, value='true', name='KeepChecking'); ex(bl, sal)
gz = g.call(KML + ':EqualEqual_IntInt', 'GaveUp', 2350, 300, B='0'); link(left, gz['A'])
bz = g.branch(2300, 150, 'BrGaveUp'); link(gz['ReturnValue'], bz['Condition']); ex(bl, bz, 'else')
gcn = g.call(KSL + ':GetClassDisplayName', 'PendClassName', 2550, 300); link(pcls, gcn['Class'])
gmsg = concat(g, 2750, 300, 'CatalystDefaults: no finished ', gcn['ReturnValue'], ' found at ', ppos, ' (cancelled?)')
lgu = log(g, 2600, 150, msg_pin=gmsg, name='LogGaveUp'); ex(bz, lgu)
# after all entries: re-arm or clear the list
ag = g.get('AnyLeft', BOOL, 750, 250, name='AnyLeftGet')
ba = g.branch(800, 100, 'BrAnyLeft'); link(ag['AnyLeft'], ba['Condition']); ex(lpP, ba, 'Completed')
st2 = set_timer(g, 1050, -50, 'CheckAgain'); ex(ba, st2)
c1 = arr_fn(g, 'Array_Clear', ACLS, 1050, 150, 'ClearClass'); link(var_arr(g, 'PendClass', ACLS, 1050, 300, 'PendClassX'), c1['TargetArray']); ex(ba, c1, 'else')
c2 = arr_fn(g, 'Array_Clear', STR, 1250, 150, 'ClearPos'); link(var_arr(g, 'PendPos', STR, 1250, 300, 'PendPosX'), c2['TargetArray']); ex(c1, c2)
c3 = arr_fn(g, 'Array_Clear', VEC, 1450, 150, 'ClearLoc'); link(var_arr(g, 'PendLoc', VEC, 1450, 300, 'PendLocX'), c3['TargetArray']); ex(c2, c3)
c4 = arr_fn(g, 'Array_Clear', INT, 1650, 150, 'ClearTries'); link(var_arr(g, 'PendTries', INT, 1650, 300, 'PendTriesX'), c4['TargetArray']); ex(c3, c4)

# ---- OnBuilt: placed without a construction site -> exactly that actor
optB = api_call(g, 'ReadModOptionValue', 400, 2600, name='ReadOptBuilt', optionId=OPT_ID, fallbackValue='On'); ex(evB, optB)
optBOn = g.call(KSTR + ':EqualEqual_StrStr', 'OptOnBuilt', 650, 2800, B='On'); link(optB['ReturnValue'], optBOn['A'])
bob = g.branch(700, 2600, 'BrOptBuilt'); link(optBOn['ReturnValue'], bob['Condition']); ex(optB, bob)
curB = g.setv('Cur', ACTOR, 950, 2600, name='CurFromBuilt'); link(evB['Actor'], curB['Cur']); ex(bob, curB)

# ---- HANDLE(Cur): catalyst slot, still off -> switch on
X0, Y0 = 3200, 1800
cg = g.get('Cur', ACTOR, X0, Y0 + 250, name='CurGet')
ic = g.call('/Script/Engine.Actor:GetComponentByClass', 'IndustryComp', X0, Y0 + 400, ComponentClass=INDC); link(cg['Cur'], ic['self'])
ic['ReturnValue'].t = OBJ(INDC)
bi = g.branch(X0 + 250, Y0, 'BrIsIndustry'); link(is_valid(g, ic['ReturnValue'], X0 + 250, Y0 + 400, 'IndustryValid'), bi['Condition'])
ex(curL, bi); ex(curB, bi)
cres = g.get('m_catalystResource', NAME, X0 + 450, Y0 + 400, owner=INDC, name='CatalystRes'); link(ic['ReturnValue'], cres['self'])
hasCat = g.call(KML + ':NotEqual_NameName', 'HasCatalystSlot', X0 + 700, Y0 + 400, B='None'); link(cres['m_catalystResource'], hasCat['A'])
allow = g.get('m_allowCatalysts', BOOL, X0 + 450, Y0 + 550, owner=INDC, name='CatalystOn'); link(ic['ReturnValue'], allow['self'])
off = g.call(KML + ':Not_PreBool', 'CatalystOff', X0 + 700, Y0 + 550); link(allow['m_allowCatalysts'], off['A'])
want = g.call(KML + ':BooleanAND', 'TurnOn', X0 + 900, Y0 + 450); link(hasCat['ReturnValue'], want['A']); link(off['ReturnValue'], want['B'])
bw = g.branch(X0 + 500, Y0, 'BrTurnOn'); link(want['ReturnValue'], bw['Condition']); ex(bi, bw)
vg = g.get('View', IV_T, X0 + 700, Y0 - 300, name='ViewChk')
bv = g.branch(X0 + 750, Y0, 'BrHaveView'); link(is_valid(g, vg['View'], X0 + 750, Y0 - 200, 'ViewValid'), bv['Condition']); ex(bw, bv)
pc = g.call('/Script/Engine.GameplayStatics:GetPlayerController', 'PC', X0 + 800, Y0 + 300)
cw = g.add('/Script/UMGEditor.K2Node_CreateWidget', 'CreateView', [], X0 + 1000, Y0 + 150)
cw.pin('execute', EXEC); cw.pin('then', EXEC, out=True)
cw.pin('Class', T('class', obj=cls_ref('/Script/UMG.UserWidget')), defobj=INDV)
cw.pin('OwningPlayer', OBJ('/Script/Engine.PlayerController'))
cw.pin('ReturnValue', IV_T, out=True)
link(pc['ReturnValue'], cw['OwningPlayer']); ex(bv, cw, 'else')
sv = g.setv('View', IV_T, X0 + 1300, Y0 + 150, name='SetView'); link(cw['ReturnValue'], sv['View']); ex(cw, sv)
vg2 = g.get('View', IV_T, X0 + 1400, Y0 + 350, name='ViewCtx')
sctx = g.call(KSL + ':SetObjectPropertyByName', 'SetContext', X0 + 1550, Y0, PropertyName='Context')
link(vg2['View'], sctx['Object']); link(cg['Cur'], sctx['Value']); ex(bv, sctx); ex(sv, sctx)
mk = struct_node(g, 'Make', PA + 'HudAction', ['action', 'paramBool'], X0 + 1800, Y0 + 250, 'MakeCatalystAction')
mk.set('action', 'setCatalystUse'); mk.set('paramBool', 'true')
ra = g.call(AWB + ':ReceiveHudAction', 'SendCatalystOn', X0 + 2050, Y0); link(vg2['View'], ra['self']); link(mk['HudAction'], ra['action'])
ex(sctx, ra)
nm = g.call(KSL + ':GetDisplayName', 'BuildingName', X0 + 2050, Y0 + 300); link(cg['Cur'], nm['Object'])
rn = g.call(KSTR + ':Conv_NameToString', 'CatalystName', X0 + 2050, Y0 + 450); link(cres['m_catalystResource'], rn['InName'])
msg = concat(g, X0 + 2300, Y0 + 300, 'CatalystDefaults: ', nm['ReturnValue'], ' Catalyst (', rn['ReturnValue'], ') -> on')
l1 = log(g, X0 + 2350, Y0, msg_pin=msg, name='LogSet'); ex(ra, l1)

open(OUT + '/BP_MapLoad.txt', 'w', encoding='utf-8').write(g.text())
print('wrote', OUT)
