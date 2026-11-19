"""Blender (headless) render of the 3D metallic bevelled logo used by fanedit_stark.

  blender -b -P tools/render_logo_blender.py -a -- FONT.ttf "VICE CARTEL" /tmp/logo/l_
then pack: ffmpeg -framerate 30 -i /tmp/logo/l_%04d.png -c:v libvpx-vp9 -pix_fmt yuva420p -b:v 0 -crf 28 \
  assets/fanedit_stark/logo_vice_cartel.webm
FONT should be a heavy serif without overlapping contours (e.g. Cinzel wght=900 via
`fonttools varLib.instancer ... --remove-overlaps`). 100 frames @30fps, 1080x480 RGBA, Cycles CPU.
"""
import sys
_a = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
FONT = _a[0] if len(_a) > 0 else "Cinzel-Black.ttf"
TEXT = _a[1] if len(_a) > 1 else "VICE CARTEL"
OUT = _a[2] if len(_a) > 2 else "/tmp/logo/l_"
import bpy, math
sc=bpy.context.scene
for o in list(bpy.data.objects): bpy.data.objects.remove(o,do_unlink=True)
font=bpy.data.fonts.load(FONT)
bpy.ops.object.text_add(location=(0,0,0))
t=bpy.context.object; t.data.body=TEXT; t.data.font=font
t.data.align_x='CENTER'; t.data.align_y='CENTER'; t.data.size=1.0
t.data.extrude=0.14; t.data.bevel_depth=0.016; t.data.bevel_resolution=5; t.data.space_character=1.05
t.rotation_euler=(math.radians(90),0,0)
mat=bpy.data.materials.new('metal'); mat.use_nodes=True
b=mat.node_tree.nodes['Principled BSDF']
b.inputs['Base Color'].default_value=(0.42,0.42,0.44,1); b.inputs['Metallic'].default_value=1.0; b.inputs['Roughness'].default_value=0.24
# subtle brushed noise on roughness
nt=mat.node_tree; n=nt.nodes.new('ShaderNodeTexNoise'); n.inputs['Scale'].default_value=60
mr=nt.nodes.new('ShaderNodeMapRange'); mr.inputs['To Min'].default_value=0.14; mr.inputs['To Max'].default_value=0.30
nt.links.new(n.outputs['Fac'],mr.inputs['Value']); nt.links.new(mr.outputs['Result'],b.inputs['Roughness'])
t.data.materials.append(mat)
# animate rotation
t.rotation_euler[2]=math.radians(-14); t.keyframe_insert('rotation_euler',frame=1)
t.rotation_euler[2]=math.radians(8); t.keyframe_insert('rotation_euler',frame=100)
for fc in t.animation_data.action.fcurves:
    for k in fc.keyframe_points: k.interpolation='LINEAR'
# world: dark grey sky gradient for reflections
w=bpy.data.worlds.new('w'); sc.world=w; w.use_nodes=True
bg=w.node_tree.nodes['Background']
sky=w.node_tree.nodes.new('ShaderNodeTexSky'); sky.sky_type='NISHITA'; sky.sun_elevation=math.radians(25); sky.sun_intensity=0.2
hsv=w.node_tree.nodes.new('ShaderNodeHueSaturation'); hsv.inputs['Saturation'].default_value=0.0; hsv.inputs['Value'].default_value=0.5
w.node_tree.links.new(sky.outputs['Color'],hsv.inputs['Color']); w.node_tree.links.new(hsv.outputs['Color'],bg.inputs['Color'])
bg.inputs['Strength'].default_value=0.18
# lights
def area(name,loc,rot,energy,size):
    L=bpy.data.lights.new(name,'AREA'); L.energy=energy; L.size=size
    o=bpy.data.objects.new(name,L); sc.collection.objects.link(o); o.location=loc; o.rotation_euler=rot; return o
area('key',(-3,-4,4),(math.radians(50),0,math.radians(-35)),900,3)
area('rim',(0,4,2),(math.radians(-60),0,math.radians(180)),700,6)
sw=area('sweep',(-7,-3,0.5),(math.radians(90),0,0),2500,1.2)
sw.data.shape='RECTANGLE'; sw.data.size=0.4; sw.data.size_y=6
sw.location.x=-7; sw.keyframe_insert('location',frame=10)
sw.location.x=7; sw.keyframe_insert('location',frame=80)
# camera
cam=bpy.data.cameras.new('c'); cam.lens=68
co=bpy.data.objects.new('cam',cam); sc.collection.objects.link(co); sc.camera=co
co.location=(0,-13.5,0.9); co.rotation_euler=(math.radians(86.2),0,0)
co.keyframe_insert('location',frame=1); co.location=(0,-12.3,0.82); co.keyframe_insert('location',frame=100)
sc.render.engine='CYCLES'; sc.cycles.device='CPU'; sc.cycles.samples=48; sc.cycles.use_denoising=True
sc.render.resolution_x=1080; sc.render.resolution_y=480; sc.render.film_transparent=True
sc.view_settings.view_transform='Filmic' if 'Filmic' in [i.identifier for i in sc.view_settings.bl_rna.properties['view_transform'].enum_items] else 'AgX'
sc.view_settings.look='None'
sc.frame_start=1; sc.frame_end=100; sc.render.fps=30
sc.render.image_settings.file_format='PNG'; sc.render.image_settings.color_mode='RGBA'
sc.render.filepath=OUT

