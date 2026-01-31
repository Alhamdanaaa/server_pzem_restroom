if (
  typeof THREE === 'undefined' ||
  typeof THREE.OrbitControls === 'undefined' ||
  typeof THREE.GLTFLoader === 'undefined'
) {
  console.error('Library Three.js tidak lengkap!');
  document.getElementById('loading').innerText =
    'Gagal memuat library Three.js';
  throw new Error('Three.js dependency error');
}

initScene();

function initScene() {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xeeeeee);

  const camera = new THREE.PerspectiveCamera(
    75,
    window.innerWidth / window.innerHeight,
    0.1,
    1000
  );

  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setPixelRatio(window.devicePixelRatio);

  document
    .getElementById('canvas-container')
    .appendChild(renderer.domElement);

  const controls = new THREE.OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;

  const ambientLight = new THREE.AmbientLight(0xffffff, 0.8);
  scene.add(ambientLight);

  const dirLight = new THREE.DirectionalLight(0xffffff, 1);
  dirLight.position.set(5, 10, 7);
  scene.add(dirLight);

  const loader = new THREE.GLTFLoader();

  loader.load(
    MODEL_URL,
    function (gltf) {
      const model = gltf.scene;
      scene.add(model);

      const box = new THREE.Box3().setFromObject(model);
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());

      const maxDim = Math.max(size.x, size.y, size.z);
      const fov = camera.fov * (Math.PI / 180);
      let cameraZ = Math.abs((maxDim / 2) / Math.tan(fov / 2));
      cameraZ *= 1.5;

      camera.position.set(center.x, center.y, cameraZ);
      camera.lookAt(center);

      controls.target.copy(center);
      controls.update();

      document.getElementById('loading').style.display = 'none';
    },
    function (xhr) {
      if (xhr.lengthComputable) {
        console.log((xhr.loaded / xhr.total) * 100 + '% loaded');
      }
    },
    function (error) {
      console.error('Gagal load model:', error);
      document.getElementById('loading').innerText =
        'Gagal memuat model 3D';
    }
  );

  window.addEventListener('resize', () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  });

  function animate() {
    requestAnimationFrame(animate);
    controls.update();
    renderer.render(scene, camera);
  }

  animate();
}
