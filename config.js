// Firebase 콘솔 > 프로젝트 설정 > 내 앱(웹) 의 구성값을 그대로 붙여넣기.
// 웹 API 키는 공개되어도 무방하며, 실제 접근 통제는 database.rules.json 의 UID 규칙이 담당한다.
window.PA_CONFIG = {
  firebase: {
    apiKey: "AIzaSyA2LBZMHgslX41qmbjZ0jKh1KJT-iKz5bY",
    authDomain: "qcells-tandem-ops.firebaseapp.com",
    databaseURL: "https://qcells-tandem-ops-default-rtdb.firebaseio.com",
    projectId: "qcells-tandem-ops",
    storageBucket: "qcells-tandem-ops.firebasestorage.app",
    messagingSenderId: "909705800417",
    appId: "1:909705800417:web:1d20fff5bb8a566ce0e5a0"
  },
  // 대시보드 상단 '지금 수집' 버튼이 여는 GitHub Actions 페이지
  actionsUrl: "https://github.com/jin-spec-0011/4_Man_Tandem_Log/actions/workflows/daily.yml"
};
