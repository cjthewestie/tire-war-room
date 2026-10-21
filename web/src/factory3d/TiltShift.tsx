// Minimotive's tilt-shift: render to a multisampled target, blur everything outside a horizontal focus band with
// two separable Gaussian passes, then tone-map in the output pass. Turned off, it renders straight to the screen.
import { useFrame, useThree } from '@react-three/fiber'
import { useEffect, useMemo } from 'react'
import * as THREE from 'three'
import { EffectComposer } from 'three/examples/jsm/postprocessing/EffectComposer.js'
import { OutputPass } from 'three/examples/jsm/postprocessing/OutputPass.js'
import { RenderPass } from 'three/examples/jsm/postprocessing/RenderPass.js'
import { ShaderPass } from 'three/examples/jsm/postprocessing/ShaderPass.js'

const TiltShiftShader = {
  uniforms: {
    tDiffuse: { value: null }, dir: { value: new THREE.Vector2(1, 0) }, res: { value: new THREE.Vector2(1, 1) },
    amount: { value: 2.0 }, focus: { value: 0.5 }, band: { value: 0.2 },
  },
  vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position,1.0); }',
  fragmentShader: `uniform sampler2D tDiffuse; uniform vec2 dir; uniform vec2 res; uniform float amount; uniform float focus; uniform float band; varying vec2 vUv;
    void main(){
      float d = smoothstep(band, band + 0.34, abs(vUv.y - focus));
      vec2 step = dir / res * amount * d;
      vec4 c = texture2D(tDiffuse, vUv) * 0.2270270270;
      c += (texture2D(tDiffuse, vUv + step * 1.3846153846) + texture2D(tDiffuse, vUv - step * 1.3846153846)) * 0.3162162162;
      c += (texture2D(tDiffuse, vUv + step * 3.2307692308) + texture2D(tDiffuse, vUv - step * 3.2307692308)) * 0.0702702703;
      gl_FragColor = c;
    }`,
}

/** Takes over rendering (useFrame priority 1). Light blur: the focus band covers the figure and nearby labels. */
export function TiltShift({ enabled }: { enabled: boolean }) {
  const { gl, scene, camera, size, viewport } = useThree()
  const { composer, blurs } = useMemo(() => {
    const rt = new THREE.WebGLRenderTarget(1, 1, { type: THREE.HalfFloatType, samples: 2 })
    const composer = new EffectComposer(gl, rt)
    composer.addPass(new RenderPass(scene, camera))
    // one horizontal + one vertical pass; 1.8 ≈ the old 1.6 then 0.8 pair (√(1.6² + 0.8²)), so the blur reads the same
    const blurs = [0, 1].map((i) => {
      const p = new ShaderPass(TiltShiftShader)
      p.uniforms.dir.value = i % 2 ? new THREE.Vector2(0, 1) : new THREE.Vector2(1, 0)
      p.uniforms.amount.value = 1.8
      composer.addPass(p)
      return p
    })
    composer.addPass(new OutputPass())
    return { composer, blurs }
  }, [gl, scene, camera])
  useEffect(() => {
    composer.setPixelRatio(viewport.dpr)
    composer.setSize(size.width, size.height)
    blurs.forEach((p) => p.uniforms.res.value.set(size.width, size.height))
  }, [composer, blurs, size.width, size.height, viewport.dpr])
  useEffect(() => () => composer.dispose(), [composer])
  useFrame(() => {
    if (enabled) composer.render()
    else gl.render(scene, camera)
  }, 1)
  return null
}
