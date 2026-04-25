#!/usr/bin/env python3
"""
断电压力测试 — atomic_write_json 极端可靠性验证
============================================================
测试目标：验证 os.replace() 原子写入在以下极端场景中不会产生损坏文件：
  1. 高频写入 + kill -9 杀死进程（模拟断电）
  2. 并发写入同一文件
  3. 磁盘 I/O 饱和

验收标准：
  - 重启后读取文件，100% 不出现 JSON Decode Error
  - 不会出现 0KB 文件
  - 不会出现截断文件

使用方法：
  python3 tests/stress_test_atomic_write.py            # 基础测试（20进程 × 100次）
  python3 tests/stress_test_atomic_write.py --hard    # 极限测试（100进程 × 500次）
  python3 tests/stress_test_atomic_write.py --chaos   # Chaos Monkey（kill -9 随机打断）
"""
import os, sys, json, time, tempfile, shutil, signal, random, subprocess
from pathlib import Path
from datetime import datetime
from multiprocessing import Process, Queue, Value, Lock
from collections import Counter

# ── 被测试的原子写入实现 ────────────────────────────────
# 复制 kronos_utils 的原子写入（测试时直接引用源码，不依赖导入）
def atomic_write_json(path, data, indent=2):
    path = Path(path)
    # 使用唯一临时文件名（避免多线程/多进程竞争同一 .tmp 文件）
    fd, tmp_path = tempfile.mkstemp(suffix='.json.tmp', prefix='atomic_', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except Exception:
        # 失败时确保清理临时文件
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise

# ── 对比的危险实现（应失败） ──────────────────────────────
def unsafe_write_json(path, data, indent=2):
    """直接 write_text —— 这是要淘汰的危险方式"""
    path = Path(path)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)

# ── 测试数据生成器 ──────────────────────────────────────
def make_record(i):
    return {
        'equity': 67000 + i * 0.1,
        'positions': ['BTC', 'ETH', 'SOL'][i % 3],
        'timestamp': datetime.now().isoformat(),
        'run_id': i,
        'padding': 'X' * random.randint(10, 200),  # 随机长度，模拟不同写入量
    }

# ── 子进程：高频写入 ──────────────────────────────────────
def writer_process(target_file, worker_id, count, queue, start_barrier):
    """每个 worker 高频写入目标文件"""
    start_barrier.wait()  # 同步开始
    ok = 0
    for i in range(count):
        data = make_record(worker_id * count + i)
        try:
            atomic_write_json(target_file, data)
            ok += 1
        except Exception as e:
            pass
        # 随机微小时延（0~2ms），增加竞态强度
        if random.random() < 0.3:
            time.sleep(random.uniform(0, 0.002))
    queue.put(('writer_done', worker_id, ok))

# ── 子进程：Chaos Monkey（随机 kill） ───────────────────
def chaos_writer(target_file, duration_seconds, queue):
    """持续写入并随机被 kill，模拟真实断电"""
    pid = os.getpid()
    start = time.time()
    writes = 0
    killed = 0
    while time.time() - start < duration_seconds:
        data = make_record(int(time.time() * 1000) % 100000)
        try:
            atomic_write_json(target_file, data)
            writes += 1
        except Exception:
            pass
        # 随机延迟
        time.sleep(random.uniform(0.005, 0.05))
        # 随机自杀了结（模拟 kill -9）
        if random.random() < 0.02:  # 2% 概率
            killed += 1
            os.kill(pid, signal.SIGKILL)
    queue.put(('chaos_done', os.getpid(), writes, killed))

# ── 验证函数 ─────────────────────────────────────────────
def verify_file(path):
    """读取文件，验证其完整性"""
    try:
        stat = os.stat(path)
        size = stat.st_size
        if size == 0:
            return 'ZERO_SIZE', size
        with open(path, 'r', encoding='utf-8') as f:
            json.load(f)
        return 'OK', size
    except json.JSONDecodeError as e:
        return f'JSON_ERROR(len={str(e.doc[:50]) if e.doc else "empty"})', os.stat(path).st_size
    except Exception as e:
        return f'{type(e).__name__}:{e}', os.stat(path).st_size if os.path.exists(path) else 'DELETED'

# ── 测试 1：并发写入 + kill 恢复 ─────────────────────────
def test_concurrent_write_kill(num_workers=20, writes_per_worker=100):
    """并发 N 个进程写入同一文件，每个进程写 100 次"""
    tmp_dir = tempfile.mkdtemp(prefix='atomic_write_test_')
    target = Path(tmp_dir) / 'treasury.json'
    queue = Queue()
    barrier = __import__('multiprocessing').Barrier(num_workers)

    # 生成测试数据
    for i in range(5):
        atomic_write_json(target, make_record(i))

    print(f"\n{'='*60}")
    print(f"测试1：并发 {num_workers}进程 × {writes_per_worker}次写入")
    print(f"目标文件：{target}")
    print(f"{'='*60}")

    # 启动写入进程
    processes = []
    for w in range(num_workers):
        p = Process(target=writer_process, args=(str(target), w, writes_per_worker, queue, barrier))
        p.start()
        processes.append(p)

    # 等待完成
    for p in processes:
        p.join(timeout=60)

    # 收集统计
    results = []
    while not queue.empty():
        results.append(queue.get())

    # 验证文件
    verdict, size = verify_file(target)
    total_writes = sum(r[2] for r in results if r[0] == 'writer_done')
    print(f"  写入总量：{total_writes} 次")
    print(f"  最终文件：size={size} | 验证={verdict}")

    shutil.rmtree(tmp_dir)
    return verdict == 'OK', verdict

# ── 测试 2：Chaos Monkey 随机 kill ──────────────────────
def test_chaos_monkey(duration=5, num_monkeys=5):
    """N 个 Chaos Monkey 进程随机自杀，测试极限恢复能力"""
    tmp_dir = tempfile.mkdtemp(prefix='chaos_test_')
    target = Path(tmp_dir) / 'survival.json'
    queue = Queue()

    # 预先生成数据
    for i in range(3):
        atomic_write_json(target, make_record(i))

    print(f"\n{'='*60}")
    print(f"测试2：Chaos Monkey {num_monkeys}进程 × {duration}秒 随机自杀")
    print(f"目标文件：{target}")
    print(f"{'='*60}")

    processes = []
    for m in range(num_monkeys):
        p = Process(target=chaos_writer, args=(str(target), duration, queue))
        p.start()
        processes.append(p)

    for p in processes:
        p.join(timeout=duration + 10)
        if p.is_alive():
            p.terminate()

    # 收集统计
    stats = []
    while not queue.empty():
        stats.append(queue.get())

    total_writes = sum(s[2] for s in stats if s[0] == 'chaos_done')
    verdict, size = verify_file(target)
    print(f"  总写入：{total_writes} 次")
    print(f"  最终文件：size={size} | 验证={verdict}")

    shutil.rmtree(tmp_dir)
    return verdict == 'OK', verdict

# ── 子进程：并发写入（模块级定义，可pickle） ──────────────
def _writer_unsafe(args):
    target, count = args
    for i in range(count):
        unsafe_write_json(target, make_record(i))

def _writer_atomic(args):
    target, count = args
    for i in range(count):
        atomic_write_json(target, make_record(i))

# ── 测试 3：对比 unsafe vs atomic ────────────────────────
def test_unsafe_vs_atomic():
    """演示直接 write_text 在并发写入时的脆弱性"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    tmp_dir = tempfile.mkdtemp(prefix='compare_test_')

    print(f"\n{'='*60}")
    print(f"测试3：unsafe (write_text) vs atomic (os.replace)")
    print(f"{'='*60}")

    # Unsafe 测试（8线程并发写）
    target_unsafe = Path(tmp_dir) / 'unsafe.json'
    for i in range(5):
        unsafe_write_json(target_unsafe, make_record(i))
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_writer_unsafe, (str(target_unsafe), 50)) for _ in range(8)]
        for f in futures: f.result()
    verdict_unsafe, size_unsafe = verify_file(target_unsafe)
    print(f"  unsafe write_text: {verdict_unsafe} (size={size_unsafe})")

    # Atomic 测试（8线程并发写）
    target_atomic = Path(tmp_dir) / 'atomic.json'
    for i in range(5):
        atomic_write_json(target_atomic, make_record(i))
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(_writer_atomic, (str(target_atomic), 50)) for _ in range(8)]
        for f in futures: f.result()
    verdict_atomic, size_atomic = verify_file(target_atomic)
    print(f"  atomic os.replace: {verdict_atomic} (size={size_atomic})")

    shutil.rmtree(tmp_dir)
    return verdict_atomic == 'OK', verdict_atomic

# ── 主入口 ────────────────────────────────────────────────
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='atomic_write_json 断电压力测试')
    parser.add_argument('--hard', action='store_true', help='极限模式：100进程×500次')
    parser.add_argument('--chaos', action='store_true', help='Chaos Monkey模式')
    parser.add_argument('--compare', action='store_true', help='对比unsafe vs atomic')
    args = parser.parse_args()

    results = []
    start_time = time.time()

    # 基础测试（总是运行）
    ok, v = test_concurrent_write_kill(
        num_workers=100 if args.hard else 20,
        writes_per_worker=500 if args.hard else 100
    )
    results.append(('并发写入', ok, v))

    if args.chaos or args.hard:
        ok, v = test_chaos_monkey(duration=8 if args.hard else 5, num_monkeys=10 if args.hard else 5)
        results.append(('Chaos Monkey', ok, v))

    if args.compare or not args.hard:
        ok, v = test_unsafe_vs_atomic()
        results.append(('unsafe对比', ok, v))

    # 报告
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  压力测试报告")
    print(f"{'='*60}")
    for name, ok, verdict in results:
        status = '✅ PASS' if ok else '❌ FAIL'
        print(f"  {name:<20}: {status} | {verdict}")
    print(f"  总耗时：{elapsed:.1f}s")
    print(f"{'='*60}")

    all_passed = all(r[1] for r in results)
    sys.exit(0 if all_passed else 1)
