import copy
import os
import sys
import tempfile
from enum import Enum
from typing import Optional, Dict
from concurrent.futures import ProcessPoolExecutor, wait

from hpc.helper import mkdir, rmdir, path_join, get_hash
from hpc.hpc_job import HpcJobManager, HpcJobConfig, JobManager
from progress.lib.handler import ProgressManager, ProgressServerJobInfo

from ..common import Mode, ParamType, PatKey, ConfigKey, LoggerOutputType, TaskType
from .codec_util import copy_del_rename, memory


class _Prefix(Enum):
    """
    各个任务名的前缀
    注意：不要改变这个顺序
    """
    ENCODE = "encode"
    COPY_REC = "cp_rec"
    MERGE = "merger"
    DECODE = "decode"
    COPY_DEC = "cp_dec"
    COPY_BIN = "cp_bin"


class _PrepareInfo(object):
    """
    编码准备信息
    """

    def __init__(self, mode: Mode, who: str, email: str,
                 gen_bin: bool, gen_rec: bool, gen_dec: bool, par_enc: bool):
        self.mode = mode

        self.is_cluster = HpcJobManager.check_env()

        from codec.manifest import SupportedCodec
        self.temp_dir = getattr(SupportedCodec, ConfigKey.TMP_DIR, tempfile.gettempdir())

        if self.is_cluster:
            self.cur_dir = rf"\\{HpcJobConfig.HPC_SCHEDULER}\{os.path.abspath(os.curdir).replace(':', '')}"
        else:
            self.cur_dir = os.path.abspath(os.curdir)

        if self.is_cluster:
            self.work_dir = rf"{self.cur_dir}\{mode.value}"
        else:
            self.work_dir = str(mode.value)

        self.who = who
        self.email = email

        self.sub_dirs = dict()

        def default(ck: str):
            a = str(ck).split("_")[-1]
            return a

        for dir_type in [ConfigKey.BIN_DIR,
                         ConfigKey.REC_DIR,
                         ConfigKey.DEC_DIR,
                         ConfigKey.STDOUT_DIR,
                         ConfigKey.STDERR_DIR]:
            self.sub_dirs[dir_type] = getattr(SupportedCodec, dir_type, default(dir_type))
            if not os.path.isabs(self.sub_dirs[dir_type]):
                self.sub_dirs[dir_type] = path_join(self.sub_dirs[dir_type], self.work_dir)

        self.prefixes: Dict[str, str] = dict()
        for key in [ConfigKey.PREFIX_ENCODE, ConfigKey.PREFIX_DECODE]:
            self.prefixes[key] = getattr(SupportedCodec, key, default(key))

        self.suffixes: Dict[str, str] = dict()
        for key in [ConfigKey.SUFFIX_STDOUT, ConfigKey.SUFFIX_STDERR]:
            self.suffixes[key] = getattr(SupportedCodec, key, default(key))
        self.suffixes[ConfigKey.STDOUT_DIR] = self.suffixes[ConfigKey.SUFFIX_STDOUT]
        self.suffixes[ConfigKey.STDERR_DIR] = self.suffixes[ConfigKey.SUFFIX_STDERR]

        # parameters refinement
        if not gen_bin:
            self.sub_dirs[ConfigKey.BIN_DIR] = None
            gen_dec = False
        if not gen_rec:
            self.sub_dirs[ConfigKey.REC_DIR] = None
        if not gen_dec:
            self.sub_dirs[ConfigKey.DEC_DIR] = None

        self.gen_bin = gen_bin
        self.gen_rec = gen_rec
        self.gen_dec = gen_dec
        self.par_enc = par_enc

        if self.is_cluster:
            self.progress_backend = ProgressManager
            self.manager = HpcJobManager
            self.executor = None
        else:
            self.progress_backend = None
            self.manager = JobManager
            self.executor = ProcessPoolExecutor(max_workers=max(4, os.cpu_count() >> 1))
        self.tasks = list()

    def ensure_dirs(self):
        """
        检查并创建必要的目录
        :return:
        """
        for d in self.sub_dirs.values():
            if d is not None and len(str(d)) > 0:
                mkdir(d, self.work_dir)
        if not self.is_cluster:
            mkdir(self.temp_dir)

    def remove_dirs(self):
        """
        删除已存在的目录
        :return:
        """
        for d in self.sub_dirs.values():
            if d is not None and len(str(d)) > 0:
                rmdir(d, self.work_dir)
        if not self.is_cluster:
            rmdir(self.temp_dir)

    def get_name(self, file_name: str, idx: Optional[int], prefix: Optional[str], suffix: str):
        if prefix is None:
            prefix = ""
        else:
            prefix = f"{prefix}_"
        if self.par_enc and idx is not None and isinstance(idx, int) and idx >= 0:
            idx = f"_{idx + 1}"
        else:
            idx = ""
        return f"{prefix}{file_name}{idx}.{suffix}"


_DEFAULT_DICT = dict()
_DEFAULT_SUF = ""
_DEFAULT_DIR = ConfigKey.STDOUT_DIR
_DEFAULT_PAT = ".+"


class Codec(object):

    def _check(self, file):
        """
        从脚本所在目录或者工作目录检查给定的文件是否存在
        :param file:
        :return:
        """
        if file is not None:
            if os.path.isabs(file):
                if not os.path.exists(file):
                    raise FileNotFoundError(f"请检查文件是否存在: {file}")
            elif not os.path.exists(path_join(file, self.info.work_dir)):
                if os.path.exists(path_join(file, self.info.cur_dir)):
                    file = path_join(file, self.info.cur_dir)
                else:
                    raise FileNotFoundError(f"请检查文件是否存在: {path_join(file, self.info.work_dir)}")
        return file

    def __init__(self, name: str,
                 param_key: Optional[dict] = None,
                 suffix: str = _DEFAULT_SUF,
                 log_dir_type: str = _DEFAULT_DIR,
                 p_log_line: str = _DEFAULT_PAT,
                 p_summary_psnr_y: str = _DEFAULT_PAT,
                 p_summary_psnr_u: str = _DEFAULT_PAT,
                 p_summary_psnr_v: str = _DEFAULT_PAT,
                 p_summary_bitrate: str = _DEFAULT_PAT,
                 p_summary_encode_time: str = _DEFAULT_PAT,
                 p_summary_decode_time: str = _DEFAULT_PAT):
        self.name = name
        self.param_key = param_key if param_key else _DEFAULT_DICT
        self.suffix = suffix
        self.log_dir_type = log_dir_type

        self.pattern = dict()
        self.pattern[PatKey.Line_Psnr_Y] = p_log_line
        self.pattern[PatKey.Line_Psnr_U] = p_log_line
        self.pattern[PatKey.Line_Psnr_V] = p_log_line
        self.pattern[PatKey.Line_Bit] = p_log_line
        self.pattern[PatKey.Line_Time] = p_log_line
        self.pattern[PatKey.Summary_Psnr_Y] = p_summary_psnr_y
        self.pattern[PatKey.Summary_Psnr_U] = p_summary_psnr_u
        self.pattern[PatKey.Summary_Psnr_V] = p_summary_psnr_v
        self.pattern[PatKey.Summary_Bitrate] = p_summary_bitrate
        self.pattern[PatKey.Summary_Encode_Time] = p_summary_encode_time
        self.pattern[PatKey.Summary_Decode_Time] = p_summary_decode_time

        self.encoder: str = ""
        self.decoder: str = ""
        self.merger: str = ""
        self.info: Optional[_PrepareInfo] = None
        self.task_desc_prefix: str = ""

    def __str__(self):
        return ""

    def prepare(self, encoder, decoder, merger, mode: Mode, who: str, email: str, hashcode: bool,
                gen_bin: bool, gen_rec: bool, gen_dec: bool, par_enc: bool):
        self.encoder = encoder
        self.decoder = decoder
        self.merger = merger
        self.info = _PrepareInfo(mode=mode, who=who, email=email,
                                 gen_bin=gen_bin, gen_rec=gen_rec, gen_dec=gen_dec, par_enc=par_enc)
        if hashcode:
            try:
                seed = self._check(self.encoder)
            except FileNotFoundError:
                seed = None
            self.task_desc_prefix = f"{who}_{get_hash(seed=seed)}_{mode.value}"
        else:
            self.task_desc_prefix = f"{who}_{mode.value}"

    def _concat_command(self, param: dict):
        """
        将参数字典拼接成命令行参数的格式

        参数字典形如 {"-w ": 416}或者{"--width=":416}，根据具体的命令行配置

        注意：
            "-w "后面存在一个空格，"--width="后面存在一个等号

            因为拼接的时候是按照{key}{value}格式拼接的

        特别地，如果某个参数不存在值，应该传入空字符串作为其值，而不应该传入None，否在该参数将被忽略

        例如FFMPEG中的-psnr，应为{"-psnr":""}

        :param param: 参数字典

        :return: 拼接后的字符串
        """
        cmd = ""
        for k, v in param.items():
            if v is None:
                continue
            if isinstance(v, list) or isinstance(v, tuple):
                for vv in v:
                    if self.param_key.get(k) is not None:
                        cmd = f"{cmd} {self.param_key[k]}{vv}"
                    elif v:
                        cmd = f"{cmd} {vv}"
            else:
                if self.param_key.get(k) is not None:
                    cmd = f"{cmd} {self.param_key[k]}{v}"
                elif v:
                    cmd = f"{cmd} {v}"
        return cmd

    @staticmethod
    def uni_name(seq):
        name, width, height, fps, bit_depth, frames, ip, ts, skip, seq_dir = seq
        return rf"{name}_{width}x{height}_{fps}"

    def execute(self, seq_info: list, qp: int, job_cfg: HpcJobConfig, extra_param: dict) -> int:
        """
        执行编解码任务
        :param seq_info: 序列信息. [name, width, height, fps, bits, frames, intra_period, temporal_sampling, skip, dir]
        :param qp: 量化参数
        :param job_cfg: hpc job的信息， 见class HpcJobConfig
        :param extra_param: 额外的参数
        :return:
        """
        # 创建必要的目录
        self.info.ensure_dirs()
        self.encoder = self._check(self.encoder)
        if self.info.gen_dec:
            self.decoder = self._check(self.decoder)
        if self.info.par_enc:
            self.decoder = self._check(self.merger)
        extra_param[ParamType.CfgEncoder] = self._check(extra_param.get(ParamType.CfgEncoder))
        extra_param[ParamType.CfgSequence] = self._check(extra_param.get(ParamType.CfgSequence))

        _, width, height, fps, bit_depth, frames, ip, ts, skip, seq_dir = seq_info
        name = Codec.uni_name(seq_info)
        extra_param["name"] = name
        mem = memory(width, height)

        name_qp = f"{name}_{qp}"
        job_name = f"{self.task_desc_prefix}_{name_qp}"

        job_id, success = self.info.manager.new(jobname=job_name,
                                                priority=job_cfg.priority,
                                                emailaddress=self.info.email)

        if success:
            rcs = 1
            frames_list = list()
            skip_list = list()
            encoded_frames = skip
            if self.info.par_enc:
                rcs = (frames + skip + ip - 1) // ip
                for i in range(rcs):
                    skip_list.append(encoded_frames)
                    frames_list.append(min(ip + 1, frames + skip - encoded_frames))
                    encoded_frames += ip
            else:
                skip_list.append(skip)
                frames_list.append(frames)

            copy_bin_cmd_list = list()
            copy_rec_cmd_list = list()
            encoder_cmd_list = list()
            bitstream_list = list()

            bitstream = None

            def get_name_cmd(do, key, idx, prefix, suffix):
                if do:
                    file_name = self.info.get_name(name_qp, idx, prefix=prefix, suffix=suffix)
                    # 直接生成到管理节点，这是为了避免并行任务运行不在同一个计算节点，导致拼接失败
                    # FIXME: 升级服务器到Windows Server 2012，这样可以指定运行在同一个节点
                    if self.info.par_enc:
                        file_name = path_join(file_name, self.info.sub_dirs[key])
                        cmd = None
                    else:
                        file_name = path_join(file_name, self.info.temp_dir)
                        cmd = copy_del_rename(file_name,
                                              self.info.sub_dirs[key],
                                              None,
                                              local=not self.info.is_cluster)
                    return file_name, cmd
                else:
                    return os.devnull, None

            # 逐个片设置编码命令、拷贝重构和码流的命令
            for idx in range(rcs):
                # 设置输出码流名字及拷贝码流的命令
                bitstream, copy_bin_cmd = get_name_cmd(self.info.gen_bin,
                                                       ConfigKey.BIN_DIR,
                                                       idx, None, self.suffix)

                # 设置重构的名字及拷贝重构的命令
                reconstruction, copy_rec_cmd = get_name_cmd(self.info.gen_rec,
                                                            ConfigKey.REC_DIR,
                                                            idx, self.info.prefixes[ConfigKey.PREFIX_ENCODE], "yuv")

                # 设置编码命令
                encode_params = {
                    ParamType.CfgEncoder: extra_param.get(ParamType.CfgEncoder),
                    ParamType.CfgSequence: extra_param.get(ParamType.CfgSequence),

                    ParamType.Sequence: path_join(name + '.yuv', seq_dir),
                    ParamType.Width: width,
                    ParamType.Height: height,
                    ParamType.Fps: fps,
                    ParamType.BitDepth: bit_depth,
                    ParamType.Frames: frames_list[idx],
                    ParamType.IntraPeriod: ip,
                    ParamType.QP: qp,
                    ParamType.OutBitStream: bitstream,
                    ParamType.OutReconstruction: reconstruction,
                    ParamType.TemporalSampling: ts,
                    ParamType.SkipFrames: skip_list[idx],
                    ParamType.ExtraParam: extra_param.get(ParamType.ExtraParam)
                }
                encoder_cmd = f"{self.encoder} {self._concat_command(encode_params)}"

                # 保存这些命令
                copy_bin_cmd_list.append(copy_bin_cmd)
                copy_rec_cmd_list.append(copy_rec_cmd)
                encoder_cmd_list.append(encoder_cmd)

                # 保存码流文件名，以便后续拼接(如果存在拼接任务)
                bitstream_list.append(bitstream)

            # 构建码流拼接的命令
            if self.info.par_enc and self.info.gen_bin and len(bitstream_list) > 1:
                bitstream = self.info.get_name(name_qp, None,
                                               prefix=None,
                                               suffix=self.suffix)
                bitstream = path_join(bitstream, self.info.sub_dirs[ConfigKey.BIN_DIR])
                encode_params = {
                    ParamType.MergeInBitStream: bitstream_list,
                    ParamType.MergeOutBitStream: bitstream,
                }
                merger_cmd = f"{self.merger} {self._concat_command(encode_params)}"
            else:
                merger_cmd = None

            # 构建解码命令及拷贝解码文件至管理节点的命令
            if self.info.gen_bin and self.info.gen_dec:
                decode = self.info.get_name(name_qp, None,
                                            prefix=self.info.prefixes[ConfigKey.PREFIX_DECODE], suffix="yuv")
                decode = path_join(decode, self.info.temp_dir)
                encode_params = {
                    ParamType.DecodeYUV: decode,
                    ParamType.InBitStream: bitstream
                }
                decoder_cmd = f"{self.decoder} {self._concat_command(encode_params)}"
                if self.info.par_enc:
                    copy_dec_cmd = None
                else:
                    copy_dec_cmd = copy_del_rename(decode, self.info.sub_dirs[ConfigKey.DEC_DIR], None,
                                                   local=not self.info.is_cluster)
            else:
                decoder_cmd = None
                copy_dec_cmd = None

            depend = []
            # encode copy_rec merge decode copy_dec copy_bin
            commands = [encoder_cmd_list, copy_rec_cmd_list, merger_cmd, decoder_cmd, copy_dec_cmd, copy_bin_cmd_list]

            def unimportant_prefix(p):
                if isinstance(p, str):
                    p = _Prefix(p)
                return p in [_Prefix.COPY_REC, _Prefix.MERGE, _Prefix.COPY_DEC, _Prefix.COPY_BIN]

            for i, (prefix, cmd) in enumerate(zip([key for key in _Prefix], commands)):
                prefix = prefix.value
                prefix_key = ConfigKey.PREFIX_ENCODE if prefix == _Prefix.ENCODE else ConfigKey.PREFIX_DECODE
                if cmd is None:
                    continue
                if isinstance(cmd, str) and len(cmd) > 0:
                    if unimportant_prefix(prefix):
                        stdout = None
                        stderr = None
                    else:
                        stdout = path_join(self.info.get_name(name_qp, None,
                                                              self.info.prefixes[prefix_key],
                                                              self.info.suffixes[ConfigKey.SUFFIX_STDOUT]),
                                           self.info.sub_dirs[ConfigKey.STDOUT_DIR])

                        stderr = path_join(self.info.get_name(name_qp, None,
                                                              self.info.prefixes[prefix_key],
                                                              self.info.suffixes[ConfigKey.SUFFIX_STDERR]),
                                           self.info.sub_dirs[ConfigKey.STDERR_DIR])
                    task_name = f"{i}_{job_name}"
                    success = self.info.manager.add(job_id, cmd, name=task_name, numcores=job_cfg.cores,
                                                    workdir=self.info.work_dir, stdout=stdout, stderr=stderr,
                                                    depend=",".join(depend))
                    if success:
                        depend.append(task_name)
                elif isinstance(cmd, list) or isinstance(cmd, tuple):
                    temp_depend = copy.deepcopy(depend)
                    for j, c in enumerate(cmd):
                        if c is None or len(c) == 0:
                            continue
                        if unimportant_prefix(prefix):
                            stdout = None
                            stderr = None
                        else:
                            stdout = path_join(self.info.get_name(name_qp, j,
                                                                  self.info.prefixes[prefix_key],
                                                                  self.info.suffixes[ConfigKey.SUFFIX_STDOUT]),
                                               self.info.sub_dirs[ConfigKey.STDOUT_DIR])
                            stderr = path_join(self.info.get_name(name_qp, j,
                                                                  self.info.prefixes[prefix_key],
                                                                  self.info.suffixes[ConfigKey.SUFFIX_STDERR]),
                                               self.info.sub_dirs[ConfigKey.STDERR_DIR])
                        task_name = f"{i}_{j}_{job_name}"
                        success = self.info.manager.add(job_id, c, name=task_name, numcores=job_cfg.cores,
                                                        workdir=self.info.work_dir, stdout=stdout, stderr=stderr,
                                                        depend=",".join(temp_depend))
                        if success:
                            depend.append(task_name)
                    del temp_depend
            task = self.info.manager.submit(job_id, nodegroup=job_cfg.groups, requestednodes=job_cfg.nodes,
                                            memorypernode=mem, executor=self.info.executor)
            if self.info.tasks is not None:
                self.info.tasks.append(task)

            # 向进度条管理器发送当前任务的信息，以便正确地更新进度条
            if self.info.progress_backend is not None:
                track_file = None
                if self.info.par_enc:
                    for i in range(rcs):
                        track_f = path_join(self.info.get_name(name_qp, i,
                                                               self.info.prefixes[ConfigKey.PREFIX_ENCODE],
                                                               self.info.suffixes[self.log_dir_type]),
                                            self.info.sub_dirs[self.log_dir_type])
                        if track_file is None:
                            track_file = track_f
                        else:
                            track_file = f"{track_file},{track_f}"
                else:

                    track_file = path_join(self.info.get_name(name_qp, -1,
                                                              self.info.prefixes[ConfigKey.PREFIX_ENCODE],
                                                              self.info.suffixes[self.log_dir_type]),
                                           self.info.sub_dirs[self.log_dir_type])
                job_info = ProgressServerJobInfo(job_id,
                                                 (frames + ts - 1 + rcs - 1) // ts,
                                                 self.pattern[PatKey.Line_Psnr_Y],
                                                 track_file)
                self.info.progress_backend.notice(job_info)
        return job_id

    def collect_log(self, seq_names: list, qps: list, anchor: bool,
                    logger_type: LoggerOutputType = LoggerOutputType.EXCEL,
                    filename: Optional[str] = None):
        if self.info is None:
            print("please call prepare() firstly")
            return

        from .._logger.scanner import LogScanner
        from .._logger.excel_handler import ExcelHelper
        scanner = LogScanner(codec=self,
                             enc_log_dir=self.info.sub_dirs[self.log_dir_type],
                             dec_log_dir=self.info.sub_dirs[self.log_dir_type],
                             seqs=seq_names,
                             qps=qps,
                             mode=self.info.mode,
                             is_separate=self.info.par_enc)
        try:
            records = scanner.scan(
                filter_func_enc=lambda fn: str(fn).startswith(self.info.prefixes[ConfigKey.PREFIX_ENCODE]),
                filter_func_dec=lambda fn: str(fn).startswith(self.info.prefixes[ConfigKey.PREFIX_DECODE]))
            if logger_type == LoggerOutputType.STDOUT:
                scanner.output(filename=sys.stdout, is_anchor=anchor)
            elif logger_type == LoggerOutputType.STDERR:
                scanner.output(filename=sys.stderr, is_anchor=anchor)
            else:
                if logger_type == LoggerOutputType.NORMAL:
                    if filename is None or len(filename.strip()) == 0:
                        filename = input("Please Enter the Output Filename: ")
                        filename = filename.strip()
                    scanner.output(filename=filename, is_anchor=anchor)
                elif logger_type == LoggerOutputType.EXCEL:
                    if filename is None or len(filename.strip()) == 0:
                        files = os.listdir(os.curdir)
                        files = list(filter(ExcelHelper.is_excel_file, files))
                        if len(files) == 0:
                            filename = os.path.basename(os.path.abspath(os.curdir))
                            filename += ".xlsm"
                        else:
                            filename = files[Codec._get_choice("|   选择目标Excel文件   |\n-----------------------", files)]

                    scanner.output(filename, is_anchor=anchor)
            return records
        except FileNotFoundError as e:
            print("系统找不到指定的路径:", e.filename, file=sys.stderr)
            return None

    def clean_dir(self):
        self.info.remove_dirs()
        return True

    def end(self):
        if not self.info.is_cluster and self.info.executor is not None:
            wait(self.info.tasks)
            self.info.executor.shutdown()

    @staticmethod
    def _get_choice(title, menus, codes=None, default=0):
        choice = default
        if codes is None:
            codes = list(range(len(menus)))
        if len(menus) > 1:
            prompt = "\n"
            prompt += title
            prompt += "\n"
            for i, menu in zip(codes, menus):
                prompt += f"\t[{i}] {menu}"
                prompt += "\n"
            prompt += f"请选择[默认：{default}]: "
            try:
                choice = int(input(prompt))
                if choice not in codes:
                    print("Illegal Choice. Use Default:", default)
                    raise ValueError()
            except ValueError:
                choice = default
        return choice

    @staticmethod
    def get_choice():
        choice = Codec._get_choice(
            title="|   选择执行任务   |\n-------------------",
            menus=["提交编码任务", "收集基准日志", "收集测试日志", "清理过时文件", "退出"],
            codes=[1, 2, 3, 4, 0],
            default=1
        )
        return TaskType(choice)

    def go(self, encoder: str, decoder: Optional[str], merger: Optional[str],
           mode: Mode, who: str, email: str,
           gen_bin: bool, gen_rec: bool, gen_dec: bool, par_enc: bool,
           qp_list: list, seq_info: list,
           cores: int, nodes: Optional[str], groups: str, priority: int,
           cfg: str, cfg_seq: Optional[Dict[str, str]], extra_param: Optional[str], with_hash: bool = True):
        """
        此函数仅仅是为了方便外部脚本一次性将全部参数传入，直接调用，而无需过多的代码。

        :param encoder: 编码器可执行文件的路径，可以是相对目录（当前目录，工作目录），绝对目录
        :param decoder: 解码器可执行文件的路径，同上
        :param merger: 码流拼接器可执行文件路径，同上
        :param mode: 编码模式
        :param who: 使用者
        :param email: 使用者邮箱
        :param gen_bin: 是否生成码流
        :param gen_rec: 是否生成重构
        :param gen_dec: 是否解码
        :param par_enc: 是否并行编码
        :param qp_list: QP测点
        :param seq_info: 序列信息列表
        :param cores: 使用的核数
        :param nodes: 使用的节点名称
        :param groups: 使用的节点组
        :param priority: 优先级，[0-4000]
        :param cfg: 编码器当前模式的配置文件
        :param cfg_seq: 各个序列的配置文件
        :param extra_param: 额外传递给编码器的参数
        :param with_hash: HPC任务名中是否显示hash
        """
        if cfg_seq is None:
            cfg_seq = dict()
        self.prepare(encoder=encoder, decoder=decoder, merger=merger,
                     mode=mode, who=who, email=email, hashcode=with_hash,
                     gen_bin=gen_bin, gen_dec=gen_dec, gen_rec=gen_rec, par_enc=par_enc)
        choice = self.get_choice()
        if choice == TaskType.EXIT:
            exit(0)
        if choice == TaskType.ENCODE_DECODE:
            jb_cfg = HpcJobConfig(cores=cores, nodes=nodes, groups=groups, priority=priority)
            for seq in seq_info:
                [self.execute(seq, qp=qp, job_cfg=jb_cfg, extra_param={
                    ParamType.CfgEncoder: cfg,
                    ParamType.CfgSequence: cfg_seq.get(seq[0]),
                    ParamType.ExtraParam: extra_param,
                }) for qp in qp_list]
        elif choice == TaskType.CLEAN:
            print("清理完成!") if self.clean_dir() else print("清理失败!")
        else:
            anchor = choice == TaskType.SCAN_ANCHOR
            self.collect_log(seq_names=[self.uni_name(seq) for seq in seq_info], anchor=anchor, qps=qp_list)
        self.end()
