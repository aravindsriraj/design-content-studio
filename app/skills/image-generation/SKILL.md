---
name: image-generation
description: Generate original imagery with the generate_image host tool, such as illustrations, scenes, textures, hero objects, look plates for storyboards and transparent cut-outs. Not for layouts, logos, exact copy, charts or product UI.
---

# Image generation

The image model makes artwork the HTML path cannot: an engraving, a painterly sky, a hero object, a
scene, a texture, a character. Anything with the logo or exact copy is built in HTML on brand, not
left to the image model: generate the picture here without words or marks, then lay the real type
and the real logo over it in `stills` (or `thumbnails`, or a HyperFrames scene).

| The ask | Route |
| --- | --- |
| Type-forward layout, an ad with copy, a slide | `stills` (HTML, exact, free) |
| A chart, a diagram, product UI | `data-viz`, a real screenshot, or HTML |
| Illustration, scene, texture, hero object, campaign concept, "explore directions" | here |
| A finished ad around a generated illustration | here, then `stills` for copy, logo and every size |

Say so when it is the wrong tool. When a film's or an ad's proof is that the surfaces are real (real
UI, real numbers, real people), generated pixels undermine it, whoever offered the idea.

## Generate

```text
generate_image(prompt="…", path="projects/<slug>/art/hero.png",
               size="1536x1024", quality="low", n=3)
```

The tool runs on the server (Gemini Nano Banana 2.1), so no key ever enters the sandbox; never call
a provider API from a script. It writes the files (`_0`, `_1` suffixes when `n` > 1) and returns
their paths plus any refusal in Google's own words: read it and adjust the prompt. `size` becomes the
nearest supported aspect ratio, `quality="low"` is 1K (about $0.03 an image) and `"high"` is 2K
(about $0.05). Every call spends money: explore at low quality, finish once at high.

The prompt is the leverage: concrete subject, action, medium, light, composition, and "no text, no
words, no letters, no logos". Prompting craft, schemas for complex scenes and the failure modes:
[references/craft.md](references/craft.md).

## Cut-outs

The image model cannot make a transparent background. For a subject that sits on something else (a
layered design, a sticker, an object reused across grounds), generate it isolated on one flat,
even ground far from the subject's own colours, then key that ground out with `image-ops` (the
luminance key in its alpha reference):

```text
generate_image(prompt="… Isolated object centred on a flat, even pure white ground; no floor, no cast
shadow, no gradient, no text, no watermark.", path="projects/<slug>/art/cutout-src.png")
```

Check the key with `image-ops --info` (`hasAlpha`) and a `look` over a dark and a light ground before
calling it a cut-out; report it to the person as it is. A halo or a bitten edge means re-key, or
re-prompt for a ground with more contrast.

## Ground it in the brand

Read `/workspace/brand/graphic-language.md` and `voice.md` before writing a prompt. Describe the
palette in words and hex from `/workspace/brand/tokens.json` ("a calm paper-coloured ground, one small
area of the signal colour #…, no other saturated colour"), the mood the voice describes, and a visual
idea tied to the subject. A campaign illustration can depart from the brand's graphic system; it
still follows its voice. Never generate the logo, a wordmark or any real mark: models render fake
letterforms. Composite the real SVG afterwards.

**Generate the surface, composite the truth.** When a deliverable needs photoreal pixels and real
type or artwork (merchandise, a device screen, a poster in a scene), generate the blank surface
("large flat empty area, no print, no text, no logo"), then composite the verified artwork onto it with
`image-ops --composite` or an HTML layer.

## Look before delivery (two rounds at most)

Generation is a slot machine: look at every result, and do not loop forever.

1. **Explore cheap.** Two or three prompt directions at low quality, `n=2` or `3`; tile them into one
   sheet (`node /workspace/tools/review-thumb.mjs --sheet ...`) and `look` against
   [references/brand-image-rubric.md](references/brand-image-rubric.md). Name per variant what works,
   what fails and the fix; if every variant fails the same way, change the prompt, not the seed.
2. **Regenerate only for a real defect** (garbled text or marks, an uncut plate or halo, broken
   anatomy, off-brand mood). Polish is not a reason to loop.
3. **One high-quality final** of the chosen direction. If two correction rounds still leave a
   blocking defect, stop and say what is blocking rather than ship it.
4. **Compose and gate.** Hand the final to `stills` (or the film) for type, logo and sizes, lint the
   composition, look at the composed result, then deliver.

A revision to an approved image is one regeneration at the approved quality, or a deterministic edit
(`image-ops`, `stills`).

## Exploring a campaign

State two to four directions in one line each (the visual idea, the medium, the mood), probe each
cheaply, look at them side by side, then recommend one with the reason and show the runners-up before
spending on a final. Say what the exploration cost (variants times quality).

## Icons and marks

Generation can explore icon directions; it never produces the final small icon. Ask for flat,
one-colour, high-contrast silhouettes without text, pick a direction, then rebuild it as a vector on
the brand's grid and test it at 24, 48 and 300 px beside the existing set. When an approved generated
mark must become an SVG, trace it and verify the trace against the raster (render it back, stack,
diff); do not redraw it by eye.
