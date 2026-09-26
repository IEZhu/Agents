Yes. Two of these will cause problems, and one is probably fine.

## 1. The 0.8 mm arm shells are the real problem

- **Printability:** with a 0.4 mm nozzle, 0.8 mm is exactly two perimeters, and it's below the 1.2 mm FDM minimum. It will print, but there's no infill between the perimeters, so each arm is just two strands of plastic.
- **Strength:** 1.2 kg is only about 12 N, which sounds small. But the load sits on a cantilever: the arm root takes a bending moment, and it takes it all the time. Bent thin shells tend to buckle or crack at the root, especially if a cable gets tugged or someone bumps the router. On top of that, a router runs warm, and PLA under a constant load creeps (sags slowly) as it gets warmer. My guess is that 0.8 mm arms would hold at first and then droop or crack within weeks to months. I haven't calculated that; it's based on experience.
- **Fix:**
  - Make the arms solid or thick-walled, at least 3–4 mm and ideally 5–6 mm.
  - Add a fillet at the arm-to-plate root. 3–5 mm radius is a good start, and bigger is better.
  - A triangular gusset under each arm, if you have room, adds more stiffness than any slicer setting can.

## 2. The 0.3 mm house number strokes will disappear

A 0.4 mm nozzle lays down lines about 0.4–0.45 mm wide. The slicer will drop features narrower than one extrusion, or turn them into blobs. PrusaSlicer's Arachne wall generator has a minimum feature size setting, but 0.3 mm is below what it will reliably keep.

- Make the strokes **at least 0.8–1.0 mm wide** (two extrusion widths) so they come out clean.
- Emboss height of **0.6 mm or more** (3 layers at 0.2 mm) makes the number readable.
- If the number sits on a face that ends up vertical on the bed, 1 mm strokes are a safer choice.
- Debossing (cutting the number into the face) usually prints cleaner than embossing on a vertical face.

## 3. The 14 mm bridge is probably fine

A 14 mm bridge is well within what an MK4 handles in PLA or PETG with default bridging settings, so it doesn't need supports. Two things to check:

- The bridge must actually be horizontal in the orientation you print in. If it ends up as an overhang under a sloped face instead, that's a different problem.
- If the screw plate's clamp force goes through that bridged layer, give it 1–2 mm of solid material above the bridge. The first bridge layer is always a bit saggy and weak.

## 4. Print orientation matters more than any setting

FDM parts are much weaker between layers than along them. If you print the bracket with the wall plate flat on the bed and the arms pointing up, the bending load at the arm root pulls the layers apart. That's the weakest possible setup.

**Print the bracket lying on its side**, with the L-profile (plate plus arm) flat on the bed. The layer lines then run continuously through the corner and along the arm. The trade-off is that the house number will be on a vertical face, which is one more reason for the thicker strokes above.

## 5. Other things to check before exporting

- **Material:** use **PETG** rather than PLA. It resists creep better and softens at a higher temperature, which matters next to a warm router. ASA works too if you have an enclosure.
- **Screw holes:** leave clearance for the screws, about 0.2–0.3 mm over the screw diameter. Add a countersink or use a washer so the head doesn't split the layers. Put at least 3–4 mm of material around each hole, and use proper anchors if you're mounting into drywall.
- **Mesh:** run the 3D Print Toolbox checks (Solid / Intersections / Thickness) in Blender. Embossed text is the usual source of non-manifold edges and overlapping faces, so apply a Union boolean to the text rather than leaving it as a separate overlapping object.
- **Units:** confirm your scene scale so the STL comes out in mm (scene unit scale 0.001 with lengths in mm, or scale 1.0 treating 1 unit as 1 mm). A bracket that imports 1000× too small or too large is a classic Blender mistake.
- **STL export:** Blender 4.2+ uses the new built-in STL exporter (`bpy.ops.wm.stl_export`). The old `bpy.ops.export_mesh.stl` add-on operator was removed around that version. I recalled this rather than checking it, so confirm against your Blender version if you script the export.

## Slicer settings (PrusaSlicer, MK4, 0.4 mm nozzle, PETG)

| Setting | Value | Why |
|---|---|---|
| Print profile | 0.20 mm STRUCTURAL (or QUALITY) | Better layer bonding than the SPEED profiles. Profile names are from memory; pick the "structural" one if your version has it. |
| Layer height | 0.20 mm | Good balance of strength and time |
| Perimeters | 5–6 | Most of a part's strength comes from its walls |
| Top/bottom solid layers | 5 / 5 | |
| Infill | 40–50% gyroid (or 100% for small arms) | A bracket this size costs little to make nearly solid |
| Filament profile | Prusament PETG (stock temps) | Stock temperatures are tuned for the MK4 |
| Cooling | Keep PETG defaults (low fan) | Too much fan hurts layer adhesion |
| Supports | None, if oriented on its side | Only enable if the preview shows red overhangs |
| Brim | Not needed for PETG on the textured sheet | Add a 3–5 mm brim if the part is tall and narrow on the bed |
| Seam | Rear or aligned, away from the arm root | Keeps the weak seam out of the stressed area |

If you'd like, I can write a parametric `bpy` script for the bracket with the thickened arms, root fillets, gussets, debossed number and countersunk holes, with all dimensions in a `PARAMS` dict. It would include a manifold check so the output goes straight to STL.
