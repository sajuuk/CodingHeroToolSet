from codec.manifest import *
from codec.common import ConfigKey
Codec(
    "JM",
    {
        ParamType.CfgEncoder: "-d ",
        ParamType.CfgSequence: "-f ",
        ParamType.Sequence: "-p InputFile=",
        ParamType.Width: "-p SourceWidth=",
        ParamType.Height: "-p SourceHeight=",
        ParamType.Fps: "-p FrameRate=",
        ParamType.BitDepth: "-p SourceBitDepthLuma=",
        ParamType.QP: "-p QPISlice=",

        ParamType.Frames: "-p FramesToBeEncoded=",
        ParamType.IntraPeriod: "-p IntraPeriod=",
        ParamType.SkipFrames: "-p FrameSkip=",
        ParamType.TemporalSampling: None,
        ParamType.OutBitStream: "-p OutputFile=",
        ParamType.OutReconstruction: "-p ReconFile=",

        ParamType.InBitStream: "-p InputFile=",
        ParamType.DecodeYUV: "-p OutputFile=",

        ParamType.MergeInBitStream: None,
        ParamType.MergeOutBitStream: None,

        ParamType.ExtraParam: None,
    },
    "h264",
    ConfigKey.STDOUT_DIR,
    rf"\s*\d+\(\w+\)\s+(?P<{PatKey.Line_Bit}>\d+)\s+(\d+)\s+(?P<{PatKey.Line_Psnr_Y}>\d+\.\d+)\s+(?P<{PatKey.Line_Psnr_U}>\d+\.\d+)\s+(?P<{PatKey.Line_Psnr_V}>\d+\.\d+)\s+(?P<{PatKey.Line_Time}>\d+).+",
    rf"\s*Y {{ PSNR \(dB\), cSNR \(dB\), MSE }}   :\s+{{\s+(?P<{PatKey.Summary_Psnr_Y}>\d+\.\d+).+}}\s*",
    rf"\s*U {{ PSNR \(dB\), cSNR \(dB\), MSE }}   :\s+{{\s+(?P<{PatKey.Summary_Psnr_U}>\d+\.\d+).+}}\s*",
    rf"\s*V {{ PSNR \(dB\), cSNR \(dB\), MSE }}   :\s+{{\s+(?P<{PatKey.Summary_Psnr_V}>\d+\.\d+).+}}\s*",
    rf"\s*Bit rate \(kbit/s\)  @ \d+\.d+ Hz\s+:\s+(?P<{PatKey.Summary_Bitrate}>\d+\.\d+)\s*",
    rf"\s*Total encoding time for the seq.\s+:\s+(?P<{PatKey.Summary_Encode_Time}>\d+\.\d+) sec.+",
    rf"\s*Total Time:\s+(?P<{PatKey.Summary_Decode_Time}>\d+\.\d+) sec.+",
)
